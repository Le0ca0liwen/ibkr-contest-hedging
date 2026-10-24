"""Day-end miss explanation (docs/AI_OPERATOR_FEATURES.md). After each scored close the app splits the
day's miss (official close - target) into fixed drivers, deterministically, from the recorded official
closes, close marks, fills, fees and burner results; an unexplained line makes the official numbers add
up to the cent. With AI on, Google may add a plain-language explanation, anomalies worth checking and
next-day settings from a fixed menu: advice only, shown and never applied. Nothing here changes
trading state; it reads records and writes day_report / day_report_ai records."""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .ai_common import AIFeature, RateLimit, ai_error, operator_text
from .burner import option_commission, stock_commission
from .cashflows import cash_carry
from .closing import POST_CLOSE, POST_CLOSE_WINDOW
from .execution_quality import timestamp
from .models import digest
from .storage import Fill, Record
from .supervisor import Supervisor

Day = date  # a field named "date" would shadow the type inside its own annotation
CENT = Decimal("0.01")
PHASES = ("post_close", "pre_close")
DRIVERS = (  # display order; the first 11 are the attributed lines
    ("delta", "Stock and delta moves"),
    ("gamma", "Gamma (curvature)"),
    ("vega", "Implied-volatility moves"),
    ("theta", "Time decay"),
    ("higher_order", "Option repricing beyond the Greeks"),
    ("mark_basis", "IBKR marks vs. quote midpoints"),
    ("hedge_trades", "Hedge trades (fill to close)"),
    ("manual_trades", "Manual TWS trades (fill to close)"),
    ("fees", "Commissions (hedge and manual)"),
    ("burner", "Closing burner (fills and fees)"),
    ("interest", "Cash interest (modeled)"),
    ("unexplained", "Unexplained / marking residual"),
    # Totals are bold on the dashboard; no leading "=", which the CSV export would guard with "'".
    ("nav_change", "Official NAV change (total)"),
    ("benchmark_growth", "− Benchmark growth (target − previous close)"),
    ("miss", "Miss (official close − target); score = |miss|"),
)
ATTRIBUTED = tuple(k for k, _ in DRIVERS[:11])
POSITION_KEYS = ("delta", "gamma", "vega", "theta", "higher_order", "mark_basis")
SYMBOL_KEYS = (*POSITION_KEYS, "trades", "fees", "burner", "total")
SYMBOL_LABELS = {
    **dict(DRIVERS),
    "trades": "Hedge and manual trades (fill to close)",
    "fees": "Commissions (hedge and manual)",
    "total": "Symbol total",
}
# Per-contract rows for Google: today's quote and marks only. A contract's P&L (or its higher-order or
# mark-basis line) with both closes' marks gives its quantity back (pnl / multiplier / mark change).
CONTRACT_FIELDS = (
    "label",
    "kind",
    "mark_source",
    "bid",
    "ask",
    "mid_close",
    "ibkr_close",
    "iv_prev",
    "iv_close",
)
# Deterministic anomaly rules (spec section 5.3).
RESIDUAL_FLOOR, RESIDUAL_SHARE = 500.0, 0.25
MARK_TOLERANCE = 0.05  # IBKR mark outside [bid - 0.05, ask + 0.05]
WIDE_SPREAD = 0.25  # (ask - bid) / mid of a close quote
HIGHER_ORDER_LIMIT = 200.0
FORECAST_MULTIPLE, FORECAST_FLOOR = 3.0, 100.0
NUMBER = re.compile(r"[-+−]?\$?\d[\d,]*(?:\.\d+)?%?")
SUGGESTIONS = {
    "no_change": "No change",
    "burner_off": "Burner off",
    "burner_shadow": "Shadow burner",
    "burner_paper": "Allow configured paper burner",
    "autostart_paper": "Auto paper ON",
    "autostart_off": "Auto paper OFF",
    "profile_standard": "Profile STANDARD",
    "review_event_caution": "Review the event caution before the open",
}
DAY_PROMPT = (
    "Explain one contest day's score miss to a non-specialist operator. The JSON input holds a deterministic "
    "attribution the app computed from the official closing NAVs, recorded close marks, fills, fees and burner "
    "results. These numbers are authoritative: quote them as given (rounding to whole dollars is fine). Never "
    "recompute or invent figures, and never claim a cause the numbers do not show. {scoring} {close} In "
    "`explanation` (at most 180 words) say what drove the miss, largest effects first, and how large the "
    "unexplained part is. In `anomalies` list at most 4 items worth checking, taken from `checks`, `anomalies` "
    "and `contracts` (for example an option whose IBKR mark sits outside its bid/ask, a wide or stale quote, a "
    "large unexplained residual, estimated fees, or missing close marks). In `suggestions` choose at most 3 "
    "settings, only from `allowed_settings`; prefer `no_change` unless the data clearly supports a change. "
    "They are suggestions the operator may ignore. Echo `date`."
)


class Anomaly(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(min_length=1, max_length=120)
    why: str = Field(min_length=1, max_length=300)
    check: str = Field(min_length=1, max_length=300)


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setting: Literal[tuple(SUGGESTIONS)]
    reason: str = Field(min_length=1, max_length=300)


class DayExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: Day  # must equal the report date (checked by DayReporter.explain)
    explanation: str = Field(min_length=1, max_length=2000)
    anomalies: list[Anomaly] = Field(max_length=6)
    suggestions: list[Suggestion] = Field(max_length=3)

    @model_validator(mode="after")
    def menu(self):
        settings = [s.setting for s in self.suggestions]
        if len(set(settings)) != len(settings) or ("no_change" in settings and len(settings) > 1):
            raise ValueError("Suggestions must be distinct and 'no_change' only alone")
        return self


class DayReportWaiting(ValueError):  # HTTP 409: an official close is missing
    pass


class DayReportBusy(ValueError):  # HTTP 409
    pass


class DayReportLimit(ValueError):  # HTTP 429
    pass


def _number(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def cents(value):
    return Decimal(repr(float(value))).quantize(CENT)


def _usd(value):
    return None if value is None else float(cents(value))


def _positions(marks):
    result = {}
    for key, quantity in ((marks or {}).get("positions") or {}).items():
        try:
            identifier, quantity = int(key), int(quantity)
        except (TypeError, ValueError):
            continue
        if quantity:
            result[identifier] = quantity
    return result


def _quote(marks, identifier):
    quote = ((marks or {}).get("quotes") or {}).get(str(identifier))
    return quote if isinstance(quote, dict) else {}


def _ibkr(marks, identifier):
    mark = ((marks or {}).get("ibkr_marks") or {}).get(str(identifier))
    return _number(mark.get("market_price")) if isinstance(mark, dict) else None


def decision_summary(decisions):
    """Counts and the most frequent blocking reasons (numbers masked so similar reasons group) of the
    day's decisions, plus the last forecast before the close."""
    rows = sorted((d for d in decisions if isinstance(d, dict)), key=lambda d: str(d.get("at")))
    reasons = Counter(
        NUMBER.sub("#", str(r))
        for d in rows
        if not d.get("trade_eligible")
        for r in d.get("blocking_reasons") or []
    )
    last = rows[-1] if rows else None
    summary = {
        "count": len(rows),
        "eligible": sum(bool(d.get("trade_eligible")) for d in rows),
        "top_blocking_reasons": [{"reason": r, "count": n} for r, n in reasons.most_common(3)],
    }
    forecast = (
        {
            "at": last.get("at"),
            "proposed_distance": _number(last.get("proposed_distance")),
            "nav_gap": _number(last.get("nav_gap")),
        }
        if last
        else None
    )
    return summary, forecast


def attribute_day(
    *,
    manifest,
    day,
    previous_day,
    official,
    previous_official,
    target,
    marks_prev,
    marks_close,
    fills,
    burn_results,
    decisions,
    caution=None,
    data_notes=(),
):
    """Pure attribution of one scored day (spec section 5.2). Lines are USD rounded to cents;
    unexplained = round(V_D - V_P, 2) - sum(attributed lines), so the official numbers add up exactly
    and miss = attributed + unexplained - benchmark growth; score = |V_D - target| as in the ledger."""
    contracts, policy = manifest.contract_map, manifest.policy
    n = manifest.accrual_days(day)
    nav, previous_nav = Decimal(str(official["nav"])), Decimal(str(previous_official["nav"]))
    target = Decimal(str(target))
    miss = nav - target
    notes, partial = list(data_notes), bool(data_notes)
    have_prev, have_close = marks_prev is not None, marks_close is not None
    lines = defaultdict(float)
    symbols = defaultdict(lambda: defaultdict(float))
    order = {c.symbol: k for k, c in reversed(list(enumerate(manifest.contracts)))}

    def add(symbol, key, amount, line=None):
        lines[line or key] += amount
        symbols[symbol][key] += amount

    traded = defaultdict(int)
    for f in fills:
        traded[f["con_id"]] += int(f["signed_quantity"])
    q_close = _positions(marks_close)
    if have_prev or not have_close:
        q_prev = _positions(marks_prev)
    else:  # previous close marks missing: holdings reconstructed from today's marks and the fills
        q_prev = {i: q_close.get(i, 0) - traded.get(i, 0) for i in {*q_close, *traded}}
        q_prev = {i: q for i, q in q_prev.items() if q}
    identifiers = sorted({*q_prev, *q_close, *traded})
    unknown = [i for i in identifiers if i not in contracts]
    marks, rows = {}, {}
    for i in identifiers:
        c = contracts.get(i)
        if c is None:
            continue
        ip, id_ = _ibkr(marks_prev, i), _ibkr(marks_close, i)
        qp, qd = _quote(marks_prev, i), _quote(marks_close, i)
        mid_p, mid_d = _number(qp.get("mid")), _number(qd.get("mid"))
        # IBKR's marks are what the official NAV uses. Take them when they exist at every close at
        # which the contract is held, so its position and fill lines share one mark per close.
        ibkr = (
            (ip is not None or id_ is not None)
            and (ip is not None or not q_prev.get(i) or not have_prev)
            and (id_ is not None or not q_close.get(i))
        )
        marks[i] = {
            "prev": ip if ibkr and ip is not None else mid_p,
            "close": id_ if ibkr and id_ is not None else mid_d,
            "mid_prev": mid_p,
            "mid_close": mid_d,
            "qp": qp,
            "qd": qd,
        }
        rows[i] = {
            "label": c.label,
            "kind": c.kind,
            "mark_source": "ibkr" if ibkr else "mid",
            "bid": _number(qd.get("bid")),
            "ask": _number(qd.get("ask")),
            "mid_prev": mid_p,
            "mid_close": mid_d,
            "ibkr_prev": ip,
            "ibkr_close": id_,
            "iv_prev": _number(qp.get("iv")),
            "iv_close": _number(qd.get("iv")),
            "pnl": 0.0,
            "higher_order": None,
            "mark_basis": None,
        }
    unmarked, no_greeks = [], []
    if have_prev and have_close:
        for i, q in q_prev.items():
            c, mark = contracts.get(i), marks.get(i)
            if c is None:
                continue
            if mark["prev"] is None or mark["close"] is None:
                unmarked.append(c.label)
                continue
            if c.kind == "STK":
                pnl = q * (mark["close"] - mark["prev"])
                add(c.symbol, "delta", pnl)
            else:
                m = c.multiplier
                mid_p = mark["mid_prev"] if mark["mid_prev"] is not None else mark["prev"]
                mid_d = mark["mid_close"] if mark["mid_close"] is not None else mark["close"]
                move = q * m * (mid_d - mid_p)
                basis = q * m * ((mark["close"] - mid_d) - (mark["prev"] - mid_p))
                qp, qd = mark["qp"], mark["qd"]
                values = [_number(qp.get(k)) for k in ("spot", "iv", "delta", "gamma", "vega", "theta")]
                spot_d, iv_d = _number(qd.get("spot")), _number(qd.get("iv"))
                if None in (*values, spot_d, iv_d):
                    parts = (0.0, 0.0, 0.0, 0.0)
                    no_greeks.append(c.label)
                else:
                    spot_p, iv_p, delta, gamma, vega, theta = values
                    ds, dv = spot_d - spot_p, iv_d - iv_p
                    parts = (
                        q * m * delta * ds,
                        q * m * 0.5 * gamma * ds * ds,
                        q * m * vega * dv,
                        q * m * theta * n,
                    )
                for key, amount in zip(("delta", "gamma", "vega", "theta"), parts, strict=True):
                    add(c.symbol, key, amount)
                higher = move - sum(parts)
                add(c.symbol, "higher_order", higher)
                add(c.symbol, "mark_basis", basis)
                rows[i]["higher_order"], rows[i]["mark_basis"] = higher, basis
                pnl = move + basis
            rows[i]["pnl"] += pnl
    burner_refs = {"hedge:" + str(x) for r in burn_results for x in r.get("intent_ids") or []}
    estimated, estimated_amount, unmarked_fills = 0, 0.0, set()
    for f in fills:
        i, k, price = f["con_id"], int(f["signed_quantity"]), float(f["price"])
        c, ref = contracts.get(i), str(f.get("order_ref") or "")
        bucket = (
            "burner"
            if ref in burner_refs
            else "hedge_trades"
            if ref.startswith(("hedge:", "hedge-package:"))
            else "manual_trades"
        )
        if bucket == "burner" and not have_close:
            continue  # the burner line then comes from the cycle results below
        fee = _number(f.get("commission"))
        if fee is None:  # commission report pending: estimate it
            fee = (
                option_commission(policy, abs(k))
                if c is not None and c.kind == "OPT"
                else stock_commission(policy, abs(k), price)
            )
            estimated, estimated_amount = estimated + 1, estimated_amount + fee
        symbol = c.symbol if c is not None else "Other"
        add(symbol, "burner" if bucket == "burner" else "fees", -fee)
        if not have_close:
            continue
        close = marks[i]["close"] if c is not None else None
        if close is None:
            unmarked_fills.add(c.label if c is not None else f"conId {i}")
            continue
        trade = k * c.multiplier * (close - price)
        add(symbol, "burner" if bucket == "burner" else "trades", trade, line=bucket)
        rows[i]["pnl"] += trade
    if not have_close:
        for r in burn_results:
            c = contracts.get(r.get("con_id"))
            add(c.symbol if c is not None else "Other", "burner", -(_number(r.get("realized_loss")) or 0.0))
    interest = None
    cash = _number((marks_prev or {}).get("cash"))
    if have_prev and cash is not None:
        short = 0.0
        for i, q in q_prev.items():
            c = contracts.get(i)
            if c is not None and c.kind == "STK" and q < 0:
                spot = marks[i]["prev"] or _number(marks[i]["qp"].get("spot")) or 0.0
                short += abs(q) * spot
        interest = cash_carry(manifest, cash, short, n, float(previous_nav))
        lines["interest"] += interest

    position_ok = have_prev and have_close
    amounts = {}
    for key in ATTRIBUTED:
        missing = (
            (key in POSITION_KEYS and not position_ok)
            or (key in ("hedge_trades", "manual_trades") and not have_close)
            or (key == "interest" and interest is None)
        )
        amounts[key] = None if missing else cents(lines.get(key, 0.0))
    nav_change = (nav - previous_nav).quantize(CENT)
    unexplained = nav_change - sum((v for v in amounts.values() if v is not None), Decimal(0))
    miss_line = miss.quantize(CENT)
    growth = nav_change - miss_line  # = round(target - V_P, 2) up to a half-cent tie
    unexplained_label = dict(DRIVERS)["unexplained"] + (
        " (includes market moves and trades: close marks missing)"
        if not have_close
        else " (includes market moves: previous close marks missing)"
        if not have_prev
        else ""
    )
    drivers = [
        {"key": k, "label": label, "amount": float(amounts[k]) if amounts[k] is not None else None}
        for k, label in DRIVERS[:11]
    ] + [
        {"key": "unexplained", "label": unexplained_label, "amount": float(unexplained)},
        {"key": "nav_change", "label": dict(DRIVERS)["nav_change"], "amount": float(nav_change)},
        {"key": "benchmark_growth", "label": dict(DRIVERS)["benchmark_growth"], "amount": float(growth)},
        {"key": "miss", "label": dict(DRIVERS)["miss"], "amount": float(miss_line)},
    ]
    by_symbol = {}
    for symbol in sorted(symbols, key=lambda s: (order.get(s, len(order)), s)):
        values = symbols[symbol]
        by_symbol[symbol] = {
            key: None
            if (key in POSITION_KEYS and not position_ok) or (key == "trades" and not have_close)
            else _usd(values.get(key, 0.0))
            for key in SYMBOL_KEYS[:-1]
        }
        by_symbol[symbol]["total"] = _usd(sum(values.values()))

    # Checks, anomalies and context.
    mismatches = [
        contracts[i].label if i in contracts else f"conId {i}"
        for i in identifiers
        if q_prev.get(i, 0) + traded.get(i, 0) != q_close.get(i, 0)
    ]
    inventory_ok = (not mismatches) if position_ok else None
    burner_cost = sum(_number(r.get("realized_loss")) or 0.0 for r in burn_results)
    accrued = None
    if have_prev and have_close and str(marks_prev.get("date"))[:7] == str(marks_close.get("date"))[:7]:
        a_p, a_d = _number(marks_prev.get("accrued_cash")), _number(marks_close.get("accrued_cash"))
        accrued = _usd(a_d - a_p) if a_p is not None and a_d is not None else None
    nav_vs_marks = {  # official close minus the account NAV at the mark capture (timing context)
        side: _usd(float(value) - captured) if captured is not None else None
        for side, value, captured in (
            ("previous", previous_nav, _number((marks_prev or {}).get("nav"))),
            ("close", nav, _number((marks_close or {}).get("nav"))),
        )
    }
    checks = {
        "inventory_ok": inventory_ok,
        "inventory_mismatches": mismatches[:10] if position_ok else [],
        "nav_vs_marks": nav_vs_marks,
        "burner_realized_cost": _usd(burner_cost),
        "burner_line": float(amounts["burner"]),
        "accrued_cash_change": accrued,
        "fees_estimated": estimated,
        "fees_estimated_amount": _usd(estimated_amount),
    }
    decision_info, forecast = decision_summary(decisions)
    score = abs(miss)
    anomalies = []
    if abs(unexplained) > max(RESIDUAL_FLOOR, RESIDUAL_SHARE * abs(float(nav_change))):
        anomalies.append(
            {
                "code": "large_residual",
                "item": "Unexplained / marking residual",
                "detail": f"Large unexplained residual: {float(unexplained):+,.2f} USD of a "
                f"{float(nav_change):+,.2f} USD NAV change",
            }
        )
    for i, row in rows.items():
        if row["kind"] != "OPT":
            continue
        for side, mark_payload, ibkr in (
            ("previous", marks_prev, row["ibkr_prev"]),
            ("today's", marks_close, row["ibkr_close"]),
        ):
            quote = _quote(mark_payload, i)
            bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
            if ibkr is not None and bid is not None and ask is not None:
                if not bid - MARK_TOLERANCE <= ibkr <= ask + MARK_TOLERANCE:
                    anomalies.append(
                        {
                            "code": "ibkr_mark_outside_quote",
                            "item": row["label"],
                            "detail": f"IBKR mark outside the quote at the {side} close: mark {ibkr:.2f}, "
                            f"bid {bid:.2f} / ask {ask:.2f}",
                        }
                    )
        quote = _quote(marks_close, i)
        bid, ask, mid = (_number(quote.get(k)) for k in ("bid", "ask", "mid"))
        if quote and (
            quote.get("data_type") != "live"
            or (bid is not None and ask is not None and mid and (ask - bid) / mid > WIDE_SPREAD)
        ):
            anomalies.append(
                {
                    "code": "wide_or_non_live_quote",
                    "item": row["label"],
                    "detail": f"Wide or non-live close quote: bid {bid if bid is not None else '—'} / ask "
                    f"{ask if ask is not None else '—'} ({quote.get('data_type') or 'unknown'} data)",
                }
            )
        if row["higher_order"] is not None and abs(row["higher_order"]) > HIGHER_ORDER_LIMIT:
            anomalies.append(
                {
                    "code": "large_higher_order",
                    "item": row["label"],
                    "detail": f"Large repricing beyond Greeks: {row['higher_order']:+,.2f} USD",
                }
            )
    if inventory_ok is False:
        anomalies.append(
            {
                "code": "inventory_mismatch",
                "item": ", ".join(mismatches[:5]),
                "detail": "Holdings changed without matching fills between the two close captures",
            }
        )
    if estimated:
        anomalies.append(
            {
                "code": "fees_estimated",
                "item": "Commissions",
                "detail": f"Commissions estimated (reports pending) for {estimated} fills: "
                f"{estimated_amount:,.2f} USD",
            }
        )
    proposed = forecast and forecast.get("proposed_distance")
    if proposed is not None and float(score) > FORECAST_MULTIPLE * proposed + FORECAST_FLOOR:
        anomalies.append(
            {
                "code": "miss_above_forecast",
                "item": "Score",
                "detail": f"Miss much larger than the last forecast: score {float(score):,.2f} USD vs. "
                f"forecast {proposed:,.2f} USD",
            }
        )

    if not have_prev:
        partial = True
        notes.append(
            f"Close marks for {previous_day} are missing (the app was not running through that close, or it "
            "predates close-mark recording): market moves are in Unexplained; trades are still marked to "
            "today's close."
        )
    if not have_close:
        partial = True
        notes.append(
            f"Close marks for {day} are missing: market moves and trades are in Unexplained; only fees, "
            "burner results, interest and benchmark growth are attributed."
        )
    if unmarked or unmarked_fills:
        partial = True
        notes.append(
            "No close mark for "
            + ", ".join(sorted({*unmarked, *unmarked_fills})[:8])
            + ": their profit and loss is in Unexplained."
        )
    if unknown:
        partial = True
        notes.append(
            "Holdings or fills outside the contest contract list ("
            + ", ".join(f"conId {i}" for i in unknown[:5])
            + "): their profit and loss is in Unexplained."
        )
    if no_greeks:
        notes.append(
            "No implied volatility or Greeks at one of the closes for "
            + ", ".join(no_greeks[:8])
            + ": their whole price change is under 'Option repricing beyond the Greeks'."
        )
    if estimated:
        partial = True
        notes.append(
            f"Commission reports are pending for {estimated} fills; {estimated_amount:,.2f} USD is estimated. "
            "Press Recompute now after they arrive."
        )
    if inventory_ok is False:
        partial = True
    if interest is None:
        notes.append("Cash interest could not be modeled without the previous close's cash balance.")
    else:
        notes.append(
            "Cash interest is modeled from the configured cash, borrow and financing rates; IBKR's actual "
            "interest is already in the official NAV, so any difference is in Unexplained."
        )
    contract_rows = [
        {
            **row,
            "pnl": _usd(row["pnl"]),
            "higher_order": _usd(row["higher_order"]),
            "mark_basis": _usd(row["mark_basis"]),
        }
        for row in rows.values()
    ]
    burner_info = {
        "cycles": sum(bool(r.get("transmitted")) for r in burn_results),
        "residual": sum(r.get("status") == "RESIDUAL" for r in burn_results),
        "realized_cost": _usd(burner_cost),
        "fees_pending": sum(bool(r.get("fees_pending")) for r in burn_results),
    }
    return {
        "status": "partial" if partial else "complete",
        "official": {
            "nav": str(nav),
            "previous_nav": str(previous_nav),
            "target": str(target),
            "miss": str(miss),
            "score": str(score),
            "provenance": official.get("provenance"),
            "previous_provenance": previous_official.get("provenance"),
            "source": official.get("source"),
            "previous_source": previous_official.get("source"),
            "accrual_days": n,
            "annual_rate": str(manifest.scoring_annual_rate)
            if manifest.scoring_annual_rate is not None
            else None,
        },
        "drivers": drivers,
        "by_symbol": by_symbol,
        "contracts": contract_rows,
        "checks": checks,
        "anomalies": anomalies[:20],
        "notes": notes,
        "context": {
            "decisions": decision_info,
            "burner": burner_info,
            "caution": caution,
            "forecast": forecast,
        },
    }


def top_drivers(report, count=3):
    """Largest attributed lines (and the unexplained residual) by size, for summaries."""
    keys = {*ATTRIBUTED, "unexplained"}
    lines = [
        d
        for d in (report or {}).get("drivers") or []
        if d.get("key") in keys and isinstance(d.get("amount"), int | float) and d["amount"]
    ]
    return [
        {"label": d["label"], "amount": d["amount"]} for d in sorted(lines, key=lambda d: -abs(d["amount"]))
    ][:count]


def ai_payload(report, account):
    """The allowlisted report for Google: app-computed numbers, labels and app-written notes only;
    never the account, positions, quantities, fills, order references or execution ids. Contract
    rows carry no P&L and no previous-close marks, so no quantity can be derived from them; P&L
    stays at symbol level (by_symbol)."""

    def text(value, limit=300):
        return operator_text(value, account, limit)

    official = report.get("official") or {}
    context = report.get("context") or {}
    decisions = context.get("decisions") or {}
    burner = context.get("burner") or {}
    checks = report.get("checks") or {}
    return {
        "date": report.get("date"),
        "previous_date": report.get("previous_date"),
        "accrual_days": official.get("accrual_days"),
        "annual_rate": official.get("annual_rate"),
        "status": report.get("status"),
        "official": {
            k: official.get(k) for k in ("previous_nav", "nav", "target", "miss", "score", "provenance")
        },
        "drivers": [
            {"label": d.get("label"), "amount": d.get("amount")} for d in report.get("drivers") or []
        ],
        "by_symbol": report.get("by_symbol") or {},
        "checks": {
            **{k: checks.get(k) for k in checks if k != "inventory_mismatches"},
            "inventory_mismatches": [text(x, 80) for x in checks.get("inventory_mismatches") or []],
        },
        "anomalies": [
            {"code": a.get("code"), "item": text(a.get("item"), 120), "detail": text(a.get("detail"))}
            for a in report.get("anomalies") or []
        ],
        "notes": [text(x) for x in report.get("notes") or []],
        "contracts": [
            {k: (text(c.get(k), 80) if k == "label" else c.get(k)) for k in CONTRACT_FIELDS}
            for c in (report.get("contracts") or [])[:60]
        ],
        "context": {
            "decisions": {
                "count": decisions.get("count"),
                "eligible": decisions.get("eligible"),
                "top_blocking_reasons": [
                    {"reason": text(r.get("reason")), "count": r.get("count")}
                    for r in decisions.get("top_blocking_reasons") or []
                ],
            },
            "burner": {k: burner.get(k) for k in ("cycles", "residual", "realized_cost", "fees_pending")},
            "caution": context.get("caution"),
        },
        "allowed_settings": SUGGESTIONS,
    }


def day_prompt(manifest, api_official):
    rate = manifest.scoring_annual_rate
    scoring = Supervisor.scoring_instruction(
        {
            "scores": {
                "scoring_rule": manifest.scoring_rule,
                "annual_rate": str(rate) if rate is not None else None,
            }
        }
    )
    close = (
        "The official close is the IBKR API closing NAV."
        if api_official
        else "The official close is the ledger's effective closing NAV (an imported statement when present)."
    )
    return DAY_PROMPT.format(scoring=scoring, close=close)


class DayReporter(AIFeature):
    """Schedules, records and serves day-end reports. tick() and compute() are synchronous and
    deterministic; only explain() awaits Google, in a service-owned background task."""

    name, label = "day_report", "Day-end report AI"

    def __init__(self, service):
        super().__init__(service)
        self.last_status = "Waiting for the first scored close"
        self.reports, self.annexes = {}, {}  # id -> payload; insertion order = age (newest last)
        self.attempted = set()  # report ids whose AI explanation has started (automatic: at most once)
        self.compute_limit = RateLimit(min_interval=30)
        self.explain_limit = RateLimit(min_interval=60, per_day=20)

    @property
    def manifest(self):
        return self.service.manifest

    def load(self):
        account, contest = self.manifest.account, self.manifest.contest_id
        store = self.service.store
        reports = [r for r in store.records("day_report", account, 100) if r.get("contest") == contest]
        annexes = [a for a in store.records("day_report_ai", account, 200) if a.get("contest") == contest]
        self.reports = {r["id"]: r for r in reversed(reports)}
        self.annexes = {a["id"]: a for a in reversed(annexes)}
        self.attempted = {a.get("report_id") for a in annexes}

    def _previous(self, day):
        sessions = (self.manifest.day_zero, *self.manifest.scoring_sessions)
        return sessions[sessions.index(day) - 1]

    def settle(self, day):
        """When a day's inputs are final: in the contest after the end-of-day NAV window (20:21 ET);
        otherwise after the post-close NAV observation window (16:26 ET by default), or 6 minutes
        after the bell when late capture is off (post-close marks by then)."""
        eod = self.service.closing_recorder.end_of_day_window(day)
        if eod is not None:  # contest: the official close is read after the post-market session;
            # a later reading in its (long) retry window changes the close and recomputes the report.
            return eod[0] + timedelta(minutes=16)
        late = self.manifest.closing_capture.late_capture_minutes
        close = self.manifest.session_close(day)
        return (
            close + timedelta(minutes=late) + POST_CLOSE_WINDOW + timedelta(minutes=1)
            if late
            else (close + timedelta(minutes=6))
        )

    def _mark_id(self, day, phase):
        return digest((self.manifest.contest_id, str(day), phase))[:32]  # CloseMarkRecorder's id

    def _choose(self, day, close, existing):
        """Close marks for one day: post-close when the official close is the post-close observation,
        otherwise the pre-close capture; the other phase (with a note) if the preferred one is missing."""
        preferred = "post_close" if POST_CLOSE in str(close.get("source")) else "pre_close"
        for phase in (preferred, *(p for p in PHASES if p != preferred)):
            identifier = self._mark_id(day, phase)
            if identifier in existing:
                note = (
                    None
                    if phase == preferred
                    else f"Used the {phase.replace('_', '-')} marks for {day}: the "
                    f"{preferred.replace('_', '-')} capture is missing"
                    + (" (pre-close marks lack SPY options' 16:15 marks)." if phase == "pre_close" else ".")
                )
                return identifier, phase, note
        return None, None, None

    def _scan(self):
        """Light reads shared by the schedule and compute(): existing close-mark ids, raw fill ids and
        times, and burn-result sessions (no payloads)."""
        account, store = self.manifest.account, self.service.store
        days = (self.manifest.day_zero, *self.manifest.scoring_sessions)
        ids = [self._mark_id(d, p) for d in days for p in PHASES]
        with store.sessions() as session:
            existing = set(
                session.scalars(
                    select(Record.id).where(
                        Record.id.in_(ids), Record.category == "close_marks", Record.account == account
                    )
                ).all()
            )
            fills = session.execute(select(Fill.id, Fill.at).where(Fill.account == account)).all()
            burns = session.execute(
                select(Record.id, Record.payload["session"].as_string()).where(
                    Record.category == "burn_cycle_result", Record.account == account
                )
            ).all()
        return existing, [(i, timestamp(at)) for i, at in fills], [(i, s) for i, s in burns]

    def _window(self, day):
        """Trades booked to the day's statement: after the previous day's last booking moment
        (20:00 ET in the contest) up to this day's."""
        return self.manifest.trading_end(self._previous(day)), self.manifest.trading_end(day)

    def _digest(self, day, closes, existing, fills, burns):
        """Both official closes (value and source), the close-mark records used, the raw fill ids in
        the window and the day's burn results. Fees are left out: late commission reports alone do not
        create a new automatic report (Recompute now picks them up)."""
        previous = self._previous(day)
        start, end = self._window(day)
        return digest(
            {
                "closes": [(str(closes[d]["nav"]), closes[d]["source"]) for d in (previous, day)],
                "marks": [self._choose(d, closes[d], existing)[0] for d in (previous, day)],
                "fills": sorted(i for i, at in fills if start < at <= end),
                "burns": sorted(i for i, session in burns if session == str(day)),
            }
        )

    def _records(self, category, *conditions):
        with self.service.store.sessions() as session:
            rows = session.scalars(
                select(Record)
                .where(Record.category == category, Record.account == self.manifest.account, *conditions)
                .order_by(Record.at)
            ).all()
            return [{"id": r.id, **r.payload} for r in rows]

    def compute(self, day, trigger):
        """Record (idempotently) and return the report for scored `day`, plus whether it is new.
        Raises DayReportWaiting while either official close is missing."""
        s, m = self.service, self.manifest
        account = m.account
        if day not in m.scoring_sessions:
            raise ValueError(f"{day} is not a scored contest session")
        previous = self._previous(day)
        closes = s.ledger.close_records()
        for d in (previous, day):
            if d not in closes:
                raise DayReportWaiting(f"Waiting for the official close for {d}")
        target = s.ledger.target_for(day, closes[previous]["nav"])
        if target is None:
            raise DayReportWaiting(
                f"The scoring target for {day} is not available (formula or daily rate pending)"
            )
        existing, raw_fills, burns = self._scan()
        inputs_digest = self._digest(day, closes, existing, raw_fills, burns)
        start, end = self._window(day)
        chosen, marks, notes, valuation = {}, {}, [], []  # valuation notes are not missing data
        for d in (previous, day):
            identifier, phase, note = self._choose(d, closes[d], existing)
            marks[d] = s.store.get_record(identifier, account, "close_marks") if identifier else None
            chosen[d] = {"id": identifier, "phase": phase, "at": (marks[d] or {}).get("at")}
            notes += [note] if note else []
            # The score is the statement's: value each close at the statement's prices (or their
            # prediction), not IBKR's live marks, which the attribution otherwise prefers.
            prices, basis = s.statement_prices(d) if marks[d] else ({}, None)
            if prices:
                marks[d] = {
                    **marks[d],
                    "ibkr_marks": {str(i): {"market_price": p} for i, p in prices.items()},
                }
                chosen[d]["prices"] = basis
                valuation.append(f"{d} positions are valued at the {basis}.")
        fills = [f for f in s.store.effective_fills(account) if start < timestamp(f["at"]) <= end]
        burn_results = s.burner._refresh_fees(
            self._records("burn_cycle_result", Record.payload["session"].as_string() == str(day))
        )
        decisions = self._records(
            "decision",
            Record.at >= m.session_open(day).astimezone(UTC),
            Record.at <= end.astimezone(UTC),
        )
        result = attribute_day(
            manifest=m,
            day=day,
            previous_day=previous,
            official=closes[day],
            previous_official=closes[previous],
            target=target,
            marks_prev=marks[previous],
            marks_close=marks[day],
            fills=fills,
            burn_results=burn_results,
            decisions=decisions,
            caution=risk.approval_for(day) if (risk := getattr(s, "event_risk", None)) else None,
            data_notes=notes,
        )
        result["notes"] = valuation + list(result.get("notes") or [])
        fees_digest = digest(sorted((f["id"], str(f["commission"])) for f in fills))
        report_id = digest(("day_report", m.contest_id, str(day), inputs_digest, fees_digest))[:32]
        if report_id in self.reports:  # same inputs and fees: the recorded report stands
            return self.reports[report_id], False
        report = {
            "version": 1,
            "contest": m.contest_id,
            "date": str(day),
            "previous_date": str(previous),
            "trigger": trigger,
            "computed_at": s.now().isoformat(),
            **result,
            "inputs": {
                "marks_prev": chosen[previous],
                "marks_close": chosen[day],
                "fills": len(fills),
                "fees_estimated": result["checks"]["fees_estimated"],
                "burn_cycles": len(burn_results),
            },
            "inputs_digest": inputs_digest,
        }
        stored = s.store.get_record(report_id, account, "day_report")
        if stored is None:
            s.store.record("day_report", account, report, report_id)
        self.reports[report_id] = {"id": report_id, **(stored or report)}
        return self.reports[report_id], stored is None

    def _newest(self, day):
        return next((r for r in reversed(self.reports.values()) if r.get("date") == str(day)), None)

    def tick(self, now):
        """Every 30 s from the service's AI loop: one automatic report per settled scored day and set
        of inputs (a corrected close makes a new one), then at most one automatic AI explanation, for
        the newest settled day. Cheap: one ledger read and three light queries."""
        m, s = self.manifest, self.service
        due = [d for d in m.scoring_sessions if self.settle(d) <= now][-10:]
        if not due:
            return
        closes = s.ledger.close_records()
        ready = [d for d in due if d in closes and self._previous(d) in closes]
        if not ready:
            return
        existing, fills, burns = self._scan()
        known = {(r.get("date"), r.get("inputs_digest")) for r in self.reports.values()}
        for day in ready:
            try:
                if (str(day), self._digest(day, closes, existing, fills, burns)) in known:
                    continue
                report, new = self.compute(day, "automatic")
            except DayReportWaiting:
                continue  # e.g. a pending scoring formula: nothing to report yet
            except Exception as exc:
                s.alert(
                    "warning",
                    f"Day-end report for {day} could not be computed ({type(exc).__name__}); trading is "
                    "unaffected. Press Recompute now on Overview to retry",
                    key=("day_report_error", str(day), type(exc).__name__),
                )
                continue
            if new:
                official = report["official"]
                s.alert(
                    "info",
                    f"Day-end report for {day} ready: miss {float(official['miss']):+,.2f}, score "
                    f"{float(official['score']):,.2f}",
                    key=("day_report", str(day), report["inputs_digest"]),
                )
                self.ok(
                    f"Report for {day} ready" + ("" if s.supervisor.enabled else " (AI off: numbers only)")
                )
        latest = self._newest(ready[-1])
        if (
            latest is not None
            and s.supervisor.enabled
            and latest["id"] not in self.attempted
            and not self.lock.locked()
        ):
            self.attempted.add(latest["id"])
            s.spawn_ai(self.explain(latest["id"], "automatic"), f"day-report-ai-{latest['date']}")

    async def explain(self, report_id, trigger):
        """One AI explanation attempt for a recorded report; writes a day_report_ai annex (ok or
        failed). Advice only: the suggestions are shown, never applied."""
        async with self.lock:
            s = self.service
            report = self.reports.get(report_id)
            if report is None:
                return None
            if not s.supervisor.enabled:
                self.ok("AI off: numbers only")
                return None
            self.attempted.add(report_id)
            attempt = 1 + sum(a.get("report_id") == report_id for a in self.annexes.values())
            annex = {
                "contest": self.manifest.contest_id,
                "report_id": report_id,
                "date": report["date"],
                "trigger": trigger,
                "status": "failed",
                "model": None,
                "explanation": None,
                "anomalies": [],
                "suggestions": [],
                "advisory_only": True,
                "usage": None,
                "error": None,
            }
            try:
                reply = await asyncio.wait_for(
                    s.supervisor.structured(
                        DayExplanation,
                        instructions=day_prompt(self.manifest, s.api_close_official),
                        payload=ai_payload(report, self.manifest.account),
                        max_output_tokens=3072,
                        timeout=45,
                    ),
                    50,
                )
                if not s.supervisor.enabled:  # turned off while Google answered: the reply is dropped
                    raise ValueError("AI disabled")
                output = reply.output
                if output.date.isoformat() != report["date"]:
                    raise ValueError(f"Invalid {s.supervisor.label} response rejected")  # wrong day echoed
                annex.update(
                    status="ok",
                    model=reply.model,
                    explanation=output.explanation,
                    anomalies=[a.model_dump() for a in output.anomalies],
                    suggestions=[
                        {"setting": x.setting, "label": SUGGESTIONS[x.setting], "reason": x.reason}
                        for x in output.suggestions
                    ],
                    usage=reply.usage,
                )
                self.ok(f"Explained {report['date']}; advice only")
            except Exception as exc:  # CancelledError (shutdown) still propagates
                annex["error"] = ai_error(exc)
                if annex["error"] == "AI disabled":  # the operator turned AI off: nothing shown or alerted
                    self.ok("AI turned off: explanation discarded; numbers only")
                    return None
                self.fail(annex["error"], "The day-end report numbers are unaffected")
            annex["at"] = s.now().isoformat()
            identifier = digest(("day_report_ai", report_id, attempt))[:32]
            try:
                s.store.record("day_report_ai", self.manifest.account, annex, identifier)
            except Exception:
                pass  # shown from memory until the next restart; the numbers are unaffected
            self.annexes[identifier] = {"id": identifier, **annex}
            return self.annexes[identifier]

    def request(self, day, action):
        """Dashboard POST /api/day-report: 'compute' (1 per 30 s) records and returns the report;
        'explain' (1 per 60 s, 20 per day, AI on) starts an AI explanation in the background."""
        s, m = self.service, self.manifest
        if day is None:
            closed = [d for d in m.scoring_sessions if m.session_close(d) <= s.now()]
            if not closed:
                raise DayReportWaiting("No scored session has closed yet")
            day = closed[-1]
        if day not in m.scoring_sessions:
            raise ValueError(f"{day} is not a scored contest session")
        if action == "explain":
            if not s.supervisor.enabled:
                raise DayReportBusy(
                    "AI is off: the day-end report shows numbers only. Enable AI on Hedge decisions for an "
                    "explanation"
                )
            if self.lock.locked():
                raise DayReportBusy("Still explaining a day-end report")
            if wait := self.explain_limit.check():
                raise DayReportLimit(
                    f"Please wait {math.ceil(wait)} s before asking for another AI explanation"
                )
            report = self._newest(day) or self._compute(day)
            self.explain_limit.hit()
            self.attempted.add(report["id"])
            s.spawn_ai(self.explain(report["id"], "operator"), f"day-report-ai-{day}")
            return {"status": "started", "date": str(day), "report_id": report["id"]}
        if wait := self.compute_limit.check():
            raise DayReportLimit(f"Please wait {math.ceil(wait)} s before recomputing")
        self.compute_limit.hit()
        report = self._compute(day)
        if s.supervisor.enabled and report["id"] not in self.attempted and not self.lock.locked():
            self.attempted.add(report["id"])
            s.spawn_ai(self.explain(report["id"], "automatic"), f"day-report-ai-{day}")
        return self.view(report)

    def _compute(self, day):
        try:
            report, _ = self.compute(day, "operator")
        except DayReportWaiting:
            raise
        except Exception as exc:
            s = self.service
            s.alert(
                "warning",
                f"Day-end report for {day} could not be computed ({type(exc).__name__}); trading is unaffected",
                key=("day_report_error", str(day), type(exc).__name__),
            )
            raise DayReportBusy(
                f"Day-end report for {day} could not be computed ({type(exc).__name__})"
            ) from None
        self.ok(
            f"Report for {day} recomputed"
            + ("" if self.service.supervisor.enabled else " (AI off: numbers only)")
        )
        return report

    def view(self, report, enabled=None):
        """A report for the dashboard (without the per-contract rows) with its newest AI annex, or the
        newest annex of an earlier report for that date (ai_for_earlier)."""
        enabled = self.service.supervisor.enabled if enabled is None else enabled
        annexes = [a for a in self.annexes.values() if a.get("date") == report.get("date")]
        own = [a for a in annexes if a.get("report_id") == report["id"]]
        ai = (own or annexes or [None])[-1]
        return {
            **{k: v for k, v in report.items() if k != "contracts"},
            "ai": ai,
            "ai_for_earlier": bool(ai) and not own,
            "ai_note": None
            if ai
            else "AI off: numbers only"
            if not enabled
            else "Explaining…"
            if self.lock.locked()
            else "No AI explanation yet: press Explain with AI",
        }

    def closed_sessions(self):
        """Scored sessions whose close has passed, newest first (last 10): the day selector also offers
        those without a report yet, so Recompute now can reach a new or failed day."""
        m, now = self.manifest, self.service.now()
        return [str(d) for d in reversed(m.scoring_sessions) if m.session_close(d) <= now][:10]

    def latest(self):
        """Newest report per date for the last 10 dates (newest first), from memory only."""
        newest = {r.get("date"): r for r in self.reports.values()}
        enabled = self.service.supervisor.enabled
        return [self.view(newest[d], enabled) for d in sorted(newest, reverse=True)[:10]]

    def export_rows(self):
        """GET /api/export/day_reports: one row per (date, scope, driver) of each date's newest report,
        plus one AI row per date with an explanation."""
        rows = []
        newest = {r.get("date"): r for r in self.reports.values()}
        for day in sorted(newest):
            report = self.view(newest[day])
            official = report.get("official") or {}
            base = {
                "date": report.get("date"),
                "previous_date": report.get("previous_date"),
                "status": report.get("status"),
                "official_close": _number(official.get("nav")),
                "previous_close": _number(official.get("previous_nav")),
                "target": _number(official.get("target")),
                "miss": _number(official.get("miss")),
                "score": _number(official.get("score")),
                "report_id": report["id"],
                "computed_at": report.get("computed_at"),
            }
            for d in report.get("drivers") or []:
                rows.append(
                    {**base, "scope": "Portfolio", "driver": d.get("label"), "amount_usd": d.get("amount")}
                )
            for symbol, values in (report.get("by_symbol") or {}).items():
                for key in SYMBOL_KEYS:
                    rows.append(
                        {**base, "scope": symbol, "driver": SYMBOL_LABELS[key], "amount_usd": values.get(key)}
                    )
            ai = report.get("ai")
            if ai and ai.get("status") == "ok":
                rows.append(
                    {
                        **base,
                        "scope": "AI",
                        "driver": "AI explanation (advice only)"
                        + (" for an earlier version" if report["ai_for_earlier"] else ""),
                        "amount_usd": None,
                        "text": ai.get("explanation"),
                        "suggestions": "; ".join(
                            f"{x.get('label')}: {x.get('reason')}" for x in ai.get("suggestions") or []
                        ),
                    }
                )
        return rows

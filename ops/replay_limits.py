"""Re-solve the app's recorded decisions under alternative gross limits. Read-only research.

    .venv/bin/python ops/replay_limits.py --config config/local.contest.yaml --day 2026-10-28 \
        [--every 15] [--archives data/moomoo] [--database URL] [--output reports/replay-limits.json]

For each recorded decision on DAY (the first in every --every minutes of the regular session):
  - holdings: the latest stored snapshot at or before it;
  - quotes: the Moomoo collector line the app read then (data/moomoo/raw-YYYYMMDD.jsonl[.gz]),
    parsed by hedge/moomoo_data.py as the app parses latest.json;
  - scenarios: rebuilt by hedge/scenarios.py with the configured daily history;
  - closing target and NAV estimate: the decision's own.
Variants (all with the configured residual-delta groups), each the app's own optimizer and risk code:
  A    as configured
  B    gross cap 3.5 x NAV
  C    options counted at |delta| x notional in the gross cap (risk.gross_basis "delta")
  D    above the cap, up to 3.5 x, only trades that lower stress loss and market exposure
       (risk.gross_risk_reducing 3.5)
  C+D  both
  A-STK, C-STK, D-STK  A, C and D trading shares only (the optimizer's stock_only solve)
  O    options offsetting the mandated legs credited out of the gross cap (risk.mandate_offsets)
Each decision also records whether the live app would trade it ("eligible": OPTIMAL, risk passed
and improvement above max(policy.score_threshold, policy.uncertainty_multiplier x uncertainty)).
Each target is also valued as a trade from the decision to the close: shares at their official
close, options at the collector's last book before 16:00, less execution_cost ("market" and "cost"
separately). "close_gap" adds
that to the session's recorded close gap (the imported statement, else the 20:05 capture), as if
the book had then been held unchanged: app trades after the decision are counted and reported.
"next_value" holds the trade on through the next scored session (its close against this one's) and
"next_close_gap" adds it to that session's recorded gap: each session is scored on its own.
Never writes to the database, the archive or a broker.
"""

import argparse
import json
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from hedge.config import NY, load_manifest  # noqa: E402
from hedge.models import PortfolioSnapshot  # noqa: E402
from hedge.moomoo_archive import _lines, archive_path  # noqa: E402
from hedge.moomoo_data import MoomooData  # noqa: E402
from hedge.optimizer import HedgeOptimizer, execution_cost  # noqa: E402
from hedge.risk import RiskEngine  # noqa: E402
from hedge.scenarios import ScenarioEngine  # noqa: E402
from hedge.supervisor import PROFILES  # noqa: E402
from hedge.universe import screen_snapshot  # noqa: E402

RECEIVED = re.compile(rb'"received_at": "([^"]+)"')
BETA = "beta_delta_dollars:portfolio"


VARIANTS = {
    "A": {},
    "B": {"gross_notional": 3.5},
    "C": {"gross_basis": "delta"},
    "D": {"gross_risk_reducing": 3.5},
    "C+D": {"gross_basis": "delta", "gross_risk_reducing": 3.5},
    "A-STK": {},
    "C-STK": {"gross_basis": "delta"},
    "D-STK": {"gross_risk_reducing": 3.5},
    "O": {"mandate_offsets": True},
}
STOCK_ONLY = {"A-STK", "C-STK", "D-STK"}


def variant_manifests(m):
    return {
        name: m.model_copy(update={"risk": m.risk.model_copy(update=change)})
        for name, change in VARIANTS.items()
    }


def trade_parts(m, quotes, closes, trades):
    """(market, cost) of a trade held from the quotes' moment to the close: quantity x multiplier x
    (close - mid), and its modeled execution cost. None when a traded contract has no closing price."""
    if any(i not in closes for i in trades):
        return None
    market = sum(q * m.contract_map[i].multiplier * (closes[i] - quotes[i].mid) for i, q in trades.items())
    ids = list(trades)
    return market, execution_cost(m, quotes, ids, [trades[i] for i in ids])


def trade_value(m, quotes, closes, trades):
    """trade_parts' market less cost, or None."""
    parts = trade_parts(m, quotes, closes, trades)
    return None if parts is None else parts[0] - parts[1]


def next_value(m, closes, closes_next, trades):
    """A trade held through the next session: quantity x multiplier x (next close - close), or None."""
    if not trades or any(i not in closes or i not in closes_next for i in trades):
        return None if trades else 0.0
    return sum(q * m.contract_map[i].multiplier * (closes_next[i] - closes[i]) for i, q in trades.items())


def replay_point(
    manifests, snapshot, quotes, scenarios, target_nav, estimate, strategy, closes, closes_next=None
):
    """{variant: result} for one decision: the solve, the target's risk metrics and its value."""
    actual = snapshot.quantities()
    current = RiskEngine(manifests["A"]).evaluate(snapshot, quotes, scenarios, actual)
    results = {}
    for name, m in manifests.items():
        target = HedgeOptimizer(m).solve(
            snapshot, quotes, scenarios, target_nav, estimate, name in STOCK_ONLY, strategy
        )
        risk = RiskEngine(m).evaluate(snapshot, quotes, scenarios, target.quantities)
        trades = {
            i: target.quantities.get(i, 0) - actual.get(i, 0)
            for i in set(target.quantities) | set(actual)
            if target.quantities.get(i, 0) != actual.get(i, 0)
        }
        parts = trade_parts(m, quotes, closes, trades)
        improvement = target.current_score - target.expected_score
        threshold = max(m.policy.score_threshold, m.policy.uncertainty_multiplier * target.uncertainty)
        results[name] = {
            "status": target.status,
            "expected_score": target.expected_score,
            "current_score": target.current_score,
            "uncertainty": target.uncertainty,
            "trades": {m.contract_map[i].label: q for i, q in sorted(trades.items())},
            "risk_passed": risk.passed,
            "risk_reasons": list(risk.reasons),
            "gross": risk.metrics.get("gross_notional"),
            "stress_loss": risk.metrics.get("stress_loss"),
            "beta_delta": risk.metrics.get(BETA),
            "residual_delta": {k: v for k, v in risk.metrics.items() if k.startswith("residual_delta:")},
            "market_exposure": risk.metrics.get("market_exposure"),
            "risk_reducing_solve": target.solver_metrics.get("risk_reducing"),
            "solve_ms": target.solver_metrics.get("total_ms"),
            "value_to_close": None if parts is None else parts[0] - parts[1],
            "market": None if parts is None else parts[0],
            "cost": None if parts is None else parts[1],
            "improvement": improvement,
            "threshold": threshold,
            "eligible": target.status == "OPTIMAL" and risk.passed and improvement > threshold,
            "next_value": None if closes_next is None else next_value(m, closes, closes_next, trades),
            "gross_offset_credit": risk.metrics.get("gross_offset_credit"),
        }
    results["current"] = {
        "gross": current.metrics.get("gross_notional"),
        "stress_loss": current.metrics.get("stress_loss"),
        "beta_delta": current.metrics.get(BETA),
        "market_exposure": current.metrics.get("market_exposure"),
        "residual_delta": {k: v for k, v in current.metrics.items() if k.startswith("residual_delta:")},
    }
    return results


def _rows(connection, category, start, end):
    from sqlalchemy import text

    rows = connection.execute(
        text("SELECT at, payload FROM records WHERE category = :c AND at >= :s AND at <= :e ORDER BY at"),
        {"c": category, "s": start, "e": end},
    )
    for at, payload in rows:
        yield at, json.loads(payload) if isinstance(payload, str) else payload


def _sampled(decisions, every):
    chosen, last = [], None
    for payload in decisions:
        at = datetime.fromisoformat(payload["at"])
        if last is None or at - last >= timedelta(minutes=every):
            chosen.append(payload)
            last = at
    return chosen


def _session_closes(connection, m, archives, day):
    """(closes, recorded close gap, closing target) for a session: shares at their official close,
    options at the collector's last book before 16:00; the statement (else end-of-day capture)
    against the session's first recorded closing target."""
    closed = datetime.combine(day, time(16), NY)
    closes = {}
    try:
        line = _archive_lines(archive_path(archives, day), [closed - timedelta(seconds=1)]).get(
            closed - timedelta(seconds=1)
        )
    except FileNotFoundError:
        line = None
    if line is not None:
        closes = {i: q.mid for i, q in _quotes(m, line, closed - timedelta(seconds=1)).items()}
    for _, record in _rows(connection, "official_closes", closed, closed + timedelta(hours=8)):
        closes.update({int(i): float(v) for i, v in (record.get("closes") or {}).items()})
    statements = [
        p
        for _, p in _rows(connection, "flex_statement", closed, closed + timedelta(days=4))
        if p.get("date") == str(day)
    ]
    captures = [
        p
        for _, p in _rows(connection, "closing_capture", closed, closed + timedelta(hours=8))
        if p.get("date") == str(day) and p.get("phase") == "end_of_day" and p.get("status") == "RECORDED"
    ]
    opened = datetime.combine(day, time(4), NY)
    targets = [
        p["closing_target"]
        for _, p in _rows(connection, "decision", opened, closed)
        if p.get("closing_target") is not None
    ]
    target = targets[0] if targets else None
    nav = float(statements[-1]["total"]) if statements else float(captures[-1]["nav"]) if captures else None
    gap = None if nav is None or target is None else nav - float(target)
    return closes, gap, target, "statement" if statements else "end_of_day capture" if captures else None


def _archive_lines(path, moments):
    """{moment: the last archive line received at or before it (within 60 s)}."""
    moments, found, previous = sorted(moments), {}, None
    k = 0
    for line in _lines(path):
        match = RECEIVED.search(line[:4096] if isinstance(line, bytes) else line.encode()[:4096])
        if not match:
            continue
        received = datetime.fromisoformat(match.group(1).decode())
        while k < len(moments) and received > moments[k]:
            if previous is not None and moments[k] - previous[0] <= timedelta(seconds=60):
                found[moments[k]] = previous[1]
            k += 1
        if k >= len(moments):
            break
        previous = (received, line)
    while k < len(moments):
        if previous is not None and moments[k] - previous[0] <= timedelta(seconds=60):
            found[moments[k]] = previous[1]
        k += 1
    return found


def _quotes(m, line, now):
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as handle:
        handle.write(line if isinstance(line, bytes) else line.encode())
        path = handle.name
    try:
        return MoomooData(m, path).quotes(now)
    finally:
        Path(path).unlink(missing_ok=True)


def main(argv=None):
    from export_mark_dataset import database_url
    from sqlalchemy import create_engine

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--every", type=int, default=15, help="minutes between replayed decisions")
    parser.add_argument("--archives", default=str(ROOT / "data/moomoo"))
    parser.add_argument("--database")
    parser.add_argument("--output", default=str(ROOT / "reports/replay-limits.json"))
    args = parser.parse_args(argv)
    m = load_manifest(args.config)
    manifests = variant_manifests(m)
    day = args.day
    opened = datetime.combine(day, time(9, 30), NY)
    closed = datetime.combine(day, time(16), NY)
    engine = create_engine(database_url(args.database))
    with engine.connect() as connection:
        decisions = [
            p
            for _, p in _rows(
                connection, "decision", opened - timedelta(hours=1), closed + timedelta(hours=1)
            )
            if opened <= datetime.fromisoformat(p["at"]) < closed and p.get("closing_target") is not None
        ]
        snapshots = list(_rows(connection, "snapshot", opened - timedelta(hours=12), closed))
        executions = [
            p["value"]
            for _, p in _rows(connection, "broker_event", opened, closed)
            if p.get("event") == "execution"
        ]
        closes, close_gap, _, close_source = _session_closes(connection, m, args.archives, day)
        following = next((d for d in m.scoring_sessions if d > day), None)
        closes_next, next_gap = None, None
        if following is not None:
            closes_next, next_gap, _, _ = _session_closes(connection, m, args.archives, following)
    chosen = _sampled(decisions, args.every)
    moments = [datetime.fromisoformat(p["at"]) for p in chosen]
    lines = _archive_lines(archive_path(args.archives, day), moments)
    scenario_engine = ScenarioEngine(m)
    points = []
    for payload, moment in zip(chosen, moments, strict=True):
        line = lines.get(moment)
        held = [s for at, s in snapshots if at <= moment]
        if line is None or not held:
            points.append({"at": payload["at"], "skipped": "no archive line or snapshot"})
            continue
        try:
            snapshot = PortfolioSnapshot.model_validate(held[-1])
            quotes = _quotes(m, line, moment)
            ids = screen_snapshot(m, quotes, snapshot)
            scenarios = scenario_engine.build(snapshot, quotes, moment, ids)
            target_nav = float(payload["closing_target"])
            estimate = target_nav + float(payload["nav_gap"])
            strategy = PROFILES.get(payload.get("profile") or "STANDARD", "dynamic_l1")
            results = replay_point(
                manifests, snapshot, quotes, scenarios, target_nav, estimate, strategy, closes, closes_next
            )
        except Exception as exc:  # one unreplayable decision never stops the rest
            points.append({"at": payload["at"], "skipped": f"{type(exc).__name__}: {exc}"})
            continue
        # The rebuilt quotes against the midpoints the app recorded with this decision.
        recorded = payload.get("reference_midpoints") or {}
        drift = [abs(quotes[int(i)].mid - float(v)) for i, v in recorded.items() if int(i) in quotes]
        later = sum(1 for e in executions if datetime.fromisoformat(e["at"]) > moment)
        for name in VARIANTS:
            value = results[name]["value_to_close"]
            results[name]["close_gap"] = None if close_gap is None or value is None else close_gap + value
            later_value = results[name]["next_value"]
            results[name]["next_close_gap"] = (
                None if next_gap is None or later_value is None else next_gap + later_value
            )
        points.append(
            {
                "at": payload["at"],
                "recorded": {k: payload.get(k) for k in ("expected_score", "current_score", "uncertainty")},
                "snapshot_age_s": (moment - datetime.fromisoformat(str(held[-1]["at"]))).total_seconds(),
                "quote_match": {
                    "contracts": len(drift),
                    "max_abs_mid_diff": max(drift, default=None),
                    "missing": len(recorded) - len(drift),
                },
                "app_executions_after": later,
                "nav_gap": payload.get("nav_gap"),
                **results,
            }
        )
        print(
            moment.astimezone(NY).strftime("%H:%M"),
            " ".join(
                f"{name} {results[name]['status'][:3]} E|g| {results[name]['expected_score']:,.0f}"
                + (
                    f" close {results[name]['close_gap']:+,.0f}"
                    if results[name].get("close_gap") is not None
                    else ""
                )
                for name in VARIANTS
            ),
            f"(recorded {payload.get('expected_score', 0):,.0f})",
        )
    summary = {}
    for name in VARIANTS:
        done = [p[name] for p in points if name in p and p[name]["status"] == "OPTIMAL"]
        gaps = [abs(r["close_gap"]) for r in done if r.get("close_gap") is not None]
        summary[name] = {
            "points": len(done),
            "mean_expected_score": sum(r["expected_score"] for r in done) / len(done) if done else None,
            "mean_abs_close_gap": sum(gaps) / len(gaps) if gaps else None,
            "max_solve_ms": max((r["solve_ms"] or 0 for r in done), default=None),
            "risk_reducing_chosen": sum(1 for r in done if (r["risk_reducing_solve"] or {}).get("chosen")),
            "eligible": sum(1 for r in done if r["eligible"]),
            "eligible_value": sum(r["value_to_close"] or 0 for r in done if r["eligible"]),
            "eligible_cost": sum(r["cost"] or 0 for r in done if r["eligible"]),
        }
    report = {
        "day": str(day),
        "close_gap_recorded": close_gap,
        "close_source": close_source,
        "next_session": str(following) if following else None,
        "next_close_gap_recorded": next_gap,
        "summary": summary,
        "points": points,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({"day": str(day), "close_gap_recorded": close_gap, "summary": summary}, indent=1))


if __name__ == "__main__":
    main()

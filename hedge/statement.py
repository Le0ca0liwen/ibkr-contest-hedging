"""Predicted IBKR Activity Statement NAV: the contest's scored close (instructor, Oct 23).

The Oct 22 statement (Ending Value 983,374.21) reconciles to the cent as
    cash 872,744.50 and accrued interest 1,601.06 after the post-market close (TotalCashValue and
    AccruedCash: post-market trades book to the day)
  + shares at their regular-session close (IBKR's RTH daily bar: RKLB 51.22, SPCX 144.75)
  + options at the Options Clearing Corporation's end-of-day price (hedge/occ_marks.py), from the
    4:00 p.m. NBBO for every class, SPY's included although its options trade until 16:15: on
    Oct 23 the SPY Nov 20 770P's statement price (16.702) sits above its 16:15 ask (16.42), and on
    Oct 22 the SPY legs are within 5 cents of their 15:59:52 midpoints but 10-18 cents from their
    16:15 ones. Most legs are their closing midpoint; a wide quote out of line with its chain is
    moved inside its bid/ask (Oct 22: RKLB 55P at its 6.55 bid in 6.55/7.40).
After the close the day's scored NAV therefore no longer moves with the market: a later trade
changes it only by quantity x (mark - price) - fees. Each option mark is uncertain within its
closing bid/ask, and the spread is the uncertainty this module carries.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from .burner import option_commission, stock_commission
from .config import LATE_OPTION_CLOSE
from .marks import smooth_marks
from .moomoo_data import code_for

SETTLE = timedelta(seconds=20)  # no fill this long before a cash push, or since: cash and positions agree
MARKS_FIXED = LATE_OPTION_CLOSE + timedelta(minutes=1)  # 16:16 ET: every closing mark is known
CAPTURE_TYPES = ("live", "synthetic")
# The app's own captures, then (service._fallback_close_marks) the Moomoo collector's archive and its
# final books when the app missed the close; post_close holds model values only.
CLOSE_MARK_PHASES = ("pre_close", "options_close", "post_close", "archive_close", "final_book")
FALLBACK_BOOK_AGE = timedelta(minutes=10)


def marks_fixed_at(manifest, day):
    return manifest.session_close(day) + MARKS_FIXED


def smoothed_chain_marks(manifest, chain):
    """{Moomoo code: smoothed mark} from an option_chain record (ops/fetch_moomoo_chain.py, taken
    just before the close): each underlying's chain through hedge/marks.py, the clearing house's
    smoothed-IV method."""
    if not chain or not chain.get("options"):
        return {}
    at = datetime.fromisoformat(chain["at"])
    result = {}
    for symbol, info in (chain.get("underlyings") or {}).items():
        rows = [
            {**o, "key": o["code"]}
            for o in chain["options"]
            if o.get("underlying") == symbol and o.get("bid") is not None and o.get("ask") is not None
        ]
        spot = (info or {}).get("spot")
        if rows and spot:
            marks = smooth_marks(rows, float(spot), at, manifest.risk_free_rate)
            result.update({code: m for code, m in marks.items() if m["source"].startswith("smoothed")})
    return result


def _anchored(curve, mid, bid, ask):
    """A chain price moved onto a later book: the chain quoted moments before it, so only the
    curve's offset from that chain's own midpoint carries over, kept inside the later bid/ask."""
    offset = curve["mark"] - (curve["low"] + curve["high"]) / 2
    return min(max(mid + offset, bid), ask)


def statement_marks(manifest, day, captures, stock_closes, chains=None):
    """({con_id: {"mark", "low", "high", "mid", "source", "estimates"}}, [con_id without a mark]).

    `captures` maps close-mark phases ("pre_close" at 16:00, "options_close" at 16:15,
    "post_close" at 16:20, fallbacks "archive_close" and "final_book") to stored close_marks
    payloads; `stock_closes` maps share conIds to their regular-session close. An option takes the
    closing book of the capture at its own close; its bid and ask bound the statement's mark. With a
    chain taken at that close (`chains`: phase -> option_chain record, its "occ" the stored
    hedge/occ_marks.chain_marks, its "local" the smoothed marks when already computed) the mark is
    OCC's rebuilt price, else the local smoothed-IV price, else the midpoint, each kept inside that
    bid/ask. "estimates" holds every one that was available, for the nightly comparison with the
    statement."""
    marks, missing = {}, []
    smoothed = {
        phase: chain["local"] if "local" in (chain or {}) else smoothed_chain_marks(manifest, chain)
        for phase, chain in (chains or {}).items()
    }
    rebuilt = {phase: (chain or {}).get("occ") or {} for phase, chain in (chains or {}).items()}
    for c in manifest.contracts:
        if c.kind == "STK":
            close = stock_closes.get(c.con_id)
            if close is None:
                missing.append(c.con_id)
            else:
                value = float(close)
                marks[c.con_id] = {
                    "mark": value,
                    "low": value,
                    "high": value,
                    "mid": value,
                    "source": "regular-session close",
                }
            continue
        # The 4:00 p.m. book for every option; SPY's 16:15 book only when nothing else exists.
        for phase in ("pre_close", "archive_close", "final_book", "options_close"):
            quote = ((captures.get(phase) or {}).get("quotes") or {}).get(str(c.con_id))
            if not quote or quote.get("data_type") not in CAPTURE_TYPES:
                continue
            bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
            if bid <= 0 or ask < bid:
                continue
            mid, code = (bid + ask) / 2, code_for(c)
            mark, source, estimates = mid, f"{phase} midpoint", {"mid": mid}
            for name, curves in (("local", smoothed), ("occ", rebuilt)):  # the last one found wins
                curve = curves.get(phase, {}).get(code)
                if curve is not None:
                    estimates[name] = mark = _anchored(curve, mid, bid, ask)
                    source = f"{phase} {curve['source']}"
            marks[c.con_id] = {
                "mark": mark,
                "low": bid,
                "high": ask,
                "mid": mid,
                "source": source,
                "estimates": estimates,
            }
            break
        else:
            missing.append(c.con_id)
    return marks, missing


def statement_value(cash, accrued, quantities, marks, contracts, field="mark"):
    """Cash + accrued interest + every holding at its mark. KeyError for a holding without a mark.
    field "low"/"high" gives the bound at which every option mark is least/most favorable."""
    total = float(cash) + float(accrued or 0)
    for con_id, quantity in quantities.items():
        if not quantity:
            continue
        mark = marks[con_id]
        if field == "mark":
            price = mark["mark"]
        else:
            favorable = (quantity > 0) == (field == "high")
            price = mark["high"] if favorable else mark["low"]
        total += quantity * contracts[con_id].multiplier * price
    return total


def statement_samples(value, quantities, marks, contracts, n=64, seed=7):
    """`n` equally likely statement NAVs around `value`: each option's statement price drawn as its
    mark plus a uniform deviation the width of its closing book, kept inside that bid/ask (shares
    are exact)."""
    rng = np.random.default_rng(seed)
    draws = np.zeros(n)
    for con_id in sorted(quantities):
        quantity, mark = quantities[con_id], marks.get(con_id)
        if not quantity or mark is None or mark["high"] <= mark["low"]:
            continue
        mid = mark.get("mid", mark["mark"])
        price = np.clip(
            mark["mark"] + rng.uniform(mark["low"], mark["high"], n) - mid, mark["low"], mark["high"]
        )
        draws += quantity * contracts[con_id].multiplier * (price - mark["mark"])
    return value + draws


def fill_fee(policy, contract, execution, fees):
    """The reported commission, else its schedule estimate (reports arrive after executions)."""
    if execution.exec_id in fees:
        return float(fees[execution.exec_id])
    units = abs(execution.signed_quantity)
    if contract.kind == "OPT":
        return option_commission(policy, units)
    return stock_commission(policy, units, float(execution.price))


class StatementTracker:
    """The predicted statement NAV of one session once its marks are fixed.

    The base is a settled IBKR cash push (no fill SETTLE before it or since, so cash and positions
    agree) valued at the statement marks. Later fills roll it forward exactly: quantity x
    (mark - price) - commission. A newer settled push re-bases it and records the difference."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.day = self.base = self.base_at = None
        self.last_check = None

    def estimate(self, day, snapshot, marks, executions, fees):
        """The predicted statement NAV now, or None until a settled push arrived for `day`.
        KeyError(con_id) while a fill since the base has no mark."""
        contracts = self.manifest.contract_map
        if self.day != day:
            self.day, self.base, self.base_at, self.last_check = day, None, None, None
        quiet = not any(e.at >= snapshot.account_at - SETTLE for e in executions)
        if quiet and (self.base_at is None or snapshot.account_at > self.base_at):
            value = statement_value(
                snapshot.cash, snapshot.accrued_cash, snapshot.quantities(), marks, contracts
            )
            if self.base is not None:
                try:
                    rolled = self._rolled(marks, executions, fees)
                except KeyError:  # a fill without a mark: this settled push re-bases past it
                    rolled = None
                self.last_check = {"at": snapshot.account_at.isoformat(), "settled": value, "rolled": rolled}
            self.base, self.base_at = value, snapshot.account_at
        if self.base is None:
            return None
        return self._rolled(marks, executions, fees)

    def _rolled(self, marks, executions, fees):
        contracts, total = self.manifest.contract_map, self.base
        for e in executions:
            if e.at > self.base_at:
                contract = contracts[e.con_id]
                mark = marks[e.con_id]["mark"]
                total += e.signed_quantity * contract.multiplier * (mark - float(e.price))
                total -= fill_fee(self.manifest.policy, contract, e, fees)
        return total

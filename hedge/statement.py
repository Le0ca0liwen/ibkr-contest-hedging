"""Predicted IBKR Activity Statement NAV: the contest's scored close (instructor, Oct 23).

The Oct 22 statement (Ending Value 983,374.21) reconciles to the cent as
    cash 872,744.50 and accrued interest 1,601.06 after the post-market close (TotalCashValue and
    AccruedCash: post-market trades book to the day)
  + shares at their regular-session close (IBKR's RTH daily bar: RKLB 51.22, SPCX 144.75)
  + options at their closing NBBO midpoint: 16:00 ET, SPY options at their 16:15 close (8 of 10
    legs within a cent of our captured midpoints; one wide-spread long leg was marked at its bid).
After the close the day's scored NAV therefore no longer moves with the market: a later trade
changes it only by quantity x (mark - price) - fees. Each option mark is uncertain within its
closing bid/ask, and the spread is the uncertainty this module carries.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from .burner import option_commission, stock_commission
from .config import LATE_OPTION_CLOSE, LATE_OPTION_SYMBOLS

SETTLE = timedelta(seconds=20)  # no fill this long before a cash push, or since: cash and positions agree
MARKS_FIXED = LATE_OPTION_CLOSE + timedelta(minutes=1)  # 16:16 ET: every closing mark is known
CAPTURE_TYPES = ("live", "synthetic")


def marks_fixed_at(manifest, day):
    return manifest.session_close(day) + MARKS_FIXED


def statement_marks(manifest, day, captures, stock_closes):
    """({con_id: {"mark", "low", "high", "source"}}, [con_id without a mark]).

    `captures` maps close-mark phases ("pre_close" at 16:00, "options_close" at 16:15,
    "post_close" at 16:20) to stored close_marks payloads; `stock_closes` maps share conIds to
    their regular-session close. An option takes the closing midpoint of the capture at its own
    close; its bid and ask bound the statement's mark."""
    marks, missing = {}, []
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
                    "source": "regular-session close",
                }
            continue
        phases = (
            ("options_close", "pre_close")
            if c.symbol in LATE_OPTION_SYMBOLS
            else ("pre_close", "options_close")
        )
        for phase in phases:
            quote = ((captures.get(phase) or {}).get("quotes") or {}).get(str(c.con_id))
            if not quote or quote.get("data_type") not in CAPTURE_TYPES:
                continue
            bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
            if bid <= 0 or ask < bid:
                continue
            marks[c.con_id] = {
                "mark": (bid + ask) / 2,
                "low": bid,
                "high": ask,
                "source": f"{phase} midpoint",
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
    """`n` equally likely statement NAVs around `value`: each option mark drawn uniformly within
    its closing bid/ask (shares are exact)."""
    rng = np.random.default_rng(seed)
    draws = np.zeros(n)
    for con_id in sorted(quantities):
        quantity, mark = quantities[con_id], marks.get(con_id)
        if not quantity or mark is None or mark["high"] <= mark["low"]:
            continue
        draws += (
            quantity
            * contracts[con_id].multiplier
            * (rng.uniform(mark["low"], mark["high"], n) - mark["mark"])
        )
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
        """The predicted statement NAV now, or None until a settled push arrived for `day`."""
        contracts = self.manifest.contract_map
        if self.day != day:
            self.day, self.base, self.base_at, self.last_check = day, None, None, None
        quiet = not any(e.at >= snapshot.account_at - SETTLE for e in executions)
        if quiet and (self.base_at is None or snapshot.account_at > self.base_at):
            value = statement_value(
                snapshot.cash, snapshot.accrued_cash, snapshot.quantities(), marks, contracts
            )
            if self.base is not None:
                self.last_check = {
                    "at": snapshot.account_at.isoformat(),
                    "settled": value,
                    "rolled": self._rolled(marks, executions, fees),
                }
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

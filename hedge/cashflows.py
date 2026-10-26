"""Forecast only future cash flows. Observed AccruedCash is already in NAV.

The first version uses a cash-balance/short-collateral proxy rather than a
settlement ledger. Rate provenance and that approximation must be disclosed.
"""

from __future__ import annotations


def cash_carry(manifest, cash, short_stock_notional, calendar_days, nav, with_credit=True):
    p = manifest.policy
    eligible = max(0, cash - 1.02 * short_stock_notional - p.cash_interest_free_balance)
    credit = eligible * p.cash_rate * min(1, max(0, nav) / 100000) if with_credit else 0
    debit = max(0, -cash) * p.financing_rate
    borrowing = short_stock_notional * p.borrow_rate
    return calendar_days * (credit - debit - borrowing) / p.cash_day_count

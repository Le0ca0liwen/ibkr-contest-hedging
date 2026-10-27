"""Closing surplus burner: pure decision math (no broker, execution, ib_async or moomoo imports).

Score per session is |V_i - target_i| on the official closing NAV; gains and losses count
equally. When the forecast closing gap z (NAV minus target, per scenario) has a positive median,
spending part of it through ordinary round trips lowers E|z|. The L1-optimal spend is the
median of z: d/dL E|z - L| = 2 P(z < L) - 1 < 0 while L < median(z). The burner never plans
past the median, burns only near the close and defers to the last minutes it can still finish in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .cashflows import cash_carry
from .order_policy import spread_error


def weighted_quantile(values, weights, probability):
    """Lower weighted quantile (the add-on's convention; the median of 64 equal weights is z[31])."""
    values, weights = np.asarray(values, dtype=float), np.asarray(weights, dtype=float)
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    index = min(int(np.searchsorted(cumulative, probability * cumulative[-1], side="left")), len(values) - 1)
    return float(values[order][index])


def weighted_median(values, weights):
    return weighted_quantile(values, weights, 0.5)


def closing_gaps(manifest, scenarios, quotes, quantities, cash, nav, nav_estimate, target, bias=0.0):
    """Per-scenario closing gap z = NAV estimate - target + bias + modeled moves + cash carry.

    Prior burn losses, fees and accrued interest are already in the NAV estimate; nothing is
    subtracted twice. Same formula as the surplus add-on's evaluator.
    """
    vector = np.array([quantities.get(i, 0) for i in scenarios.ids], dtype=float)
    short = sum(
        max(0, -quantities.get(i, 0)) * quotes[i].spot
        for i in scenarios.ids
        if manifest.contract_map[i].kind == "STK"
    )
    carry = cash_carry(manifest, float(cash), short, scenarios.calendar_days, float(nav))
    return float(nav_estimate) - float(target) + float(bias) + scenarios.changes @ vector + carry


def gap_stats(z, weights):
    z, weights = np.asarray(z, dtype=float), np.asarray(weights, dtype=float)
    mean = float(weights @ z)
    return {
        "median": weighted_median(z, weights),
        "mean": mean,
        "p05": weighted_quantile(z, weights, 0.05),
        "p95": weighted_quantile(z, weights, 0.95),
        "std": float(np.sqrt(max(0.0, weights @ (z - mean) ** 2))),
        "score": float(weights @ np.abs(z)),
    }


def stock_commission(policy, quantity, price):
    """IBKR Fixed-style per-order stock commission: max(minimum, rate*shares), capped at 1% of value."""
    fee = max(policy.minimum_commission, policy.commission_share * quantity)
    return min(fee, 0.01 * quantity * price) if price > 0 else fee


def option_commission(policy, quantity):
    return max(policy.minimum_commission, policy.commission_option * quantity)


def round_trip_loss(contract, quote, quantity, policy):
    """Expected loss of buy q at the ask then sell q at the bid, with both commissions."""
    spread = max(0.0, quote.ask - quote.bid)
    if contract.kind == "OPT":
        return quantity * contract.multiplier * spread + 2 * option_commission(policy, quantity)
    return (
        quantity * spread
        + stock_commission(policy, quantity, quote.ask)
        + stock_commission(policy, quantity, quote.bid)
    )


def l1_improvement(z, weights, loss):
    """E|z| - E|z - loss|: the modeled score improvement of spending `loss` now."""
    z = np.asarray(z, dtype=float)
    return float(np.asarray(weights) @ (np.abs(z) - np.abs(z - loss)))


def hysteresis(needed, active, start_threshold, stop_threshold):
    """Engage above the start threshold; stay engaged until the median falls to the stop threshold."""
    return needed > (stop_threshold if active else start_threshold)


def plan_cap(needed, stop_threshold):
    """Largest loss a single cycle may plan: never past the median (half the stop band kept)."""
    return needed - stop_threshold / 2


def cycle_seconds(assumed, measured):
    """The assumption, or today's measured mean once three cycles completed if that is SLOWER.

    A measured plan-to-result time omits the waits between cycles (scenario rebuilds, hedge
    optimizations), so it may only lengthen the assumption: pacing never plans to burn faster.
    """
    measured = [m for m in measured if m is not None and m >= 0]
    value = max(assumed, sum(measured) / len(measured)) if len(measured) >= 3 else assumed
    return max(1.0, float(value))


def fill_efficiency(results, floor=0.25):
    """Realized / planned loss over today's transmitted cycles (IOC misses, partial fills).

    1 until three cycles were transmitted; never below `floor`, never above 1.
    """
    rows = [r for r in results if r.get("transmitted") and r.get("expected_loss")]
    if len(rows) < 3:
        return 1.0
    planned = sum(r["expected_loss"] for r in rows)
    realized = sum(max(0.0, r["realized_loss"]) for r in rows)
    return min(1.0, max(floor, realized / planned)) if planned > 0 else 1.0


def capacity_per_minute(expected_loss, seconds):
    return expected_loss * 60 / max(1.0, seconds)


def jit_waiting(needed, capacity, remaining_minutes, headroom, reserve_minutes):
    """Just-in-time pacing: wait while the remaining modeled capacity comfortably covers `needed`."""
    return needed <= headroom * capacity * max(0.0, remaining_minutes - reserve_minutes)


@dataclass(frozen=True)
class Candidate:
    con_id: int
    label: str
    kind: str
    quantity: int
    original: int
    expected_loss: float
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    multiplier: int
    quote_at: str

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def as_dict(self):
        return {**asdict(self), "mid": self.mid}


def quote_error(quote, now, max_age, demo):
    if quote is None:
        return "No quote"
    if quote.data_type != ("synthetic" if demo else "live"):
        return "Non-live quote"
    if not 0 <= (now - quote.observed_at).total_seconds() <= max_age:
        return "Stale/future quote"
    if quote.bid <= 0 or quote.ask <= 0:
        return "No two-sided book"
    if quote.ask <= quote.bid:
        return "Locked book"  # a zero modeled spread would under-price the round trip
    return None


def candidates(manifest, scenarios, quotes, quantities, now, *, demo, options_open):
    """Every round-trip candidate (stock sizes from the policy ladder; small option clips).

    Only instruments whose ORIGINAL holding is >= 0: buy-then-sell never creates a new short,
    so an exit never depends on borrow. Returns (candidates, rejected reasons by label).
    """
    policy = manifest.burner
    result, rejected = [], {}
    universe = set(scenarios.ids)
    stocks = [(s, manifest.burner_stock_id(s)) for s in policy.symbols]
    for symbol, identifier in stocks:
        if identifier is None:
            rejected[symbol] = "Not an allowlisted stock"
            continue
        c, q = manifest.contract_map[identifier], quotes.get(identifier)
        original = quantities.get(identifier, 0)
        reason = (
            "Short original holding; buy-then-sell would re-short"
            if original < 0
            else "Not in the current scenario universe"
            if identifier not in universe
            else quote_error(q, now, policy.quote_age_seconds, demo) or spread_error(manifest, c, q)
        )
        if reason:
            rejected[symbol] = reason
            continue
        for size in policy.quantities:
            if size > policy.max_shares:
                continue
            if size > min(q.ask_size, q.bid_size):
                rejected[f"{symbol}:{size}"] = "Exceeds displayed size"
                continue
            if policy.sell_first and size > original:
                rejected[f"{symbol}:{size}"] = "Sell-first size above the holding"
                continue
            signed = -size if policy.sell_first else size
            result.append(_candidate(c, q, signed, original, policy, manifest))
    if policy.option_burn and options_open:
        for identifier in manifest.allowlist:
            c = manifest.contract_map[identifier]
            if c.kind != "OPT" or identifier in manifest.mandatory or manifest.prohibited_reason(c):
                continue
            q, original = quotes.get(identifier), quantities.get(identifier, 0)
            if original < 0 or identifier not in universe:
                continue
            reason = quote_error(q, now, policy.quote_age_seconds, demo) or spread_error(manifest, c, q)
            if reason is None and q.ask - q.bid > policy.option_max_spread + 1e-9:
                reason = "Option spread above the burner cap"
            if reason is None and (q.iv is None or q.greek_at is None):
                reason = "Missing option Greeks"
            if reason:
                rejected[c.label] = reason
                continue
            for size in range(1, policy.option_max_contracts + 1):
                if size > min(q.ask_size, q.bid_size):
                    break
                result.append(_candidate(c, q, size, original, manifest.burner, manifest))
    return result, rejected


def _candidate(contract, quote, size, original, policy, manifest):
    return Candidate(
        con_id=contract.con_id,
        label=contract.label,
        kind=contract.kind,
        quantity=size,  # signed: negative for a sell-first stock cycle
        original=original,
        expected_loss=round_trip_loss(contract, quote, abs(size), manifest.policy),
        bid=quote.bid,
        ask=quote.ask,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        multiplier=contract.multiplier,
        quote_at=quote.observed_at.isoformat(),
    )


def select(options, z, weights, needed, policy, *, expense_remaining=None, risk_error=None):
    """The LARGEST candidate that never plans past the median, improves E|z| enough, fits the
    daily expense cap, and passes the injected path-risk check. Returns (candidate, improvement,
    rejected reasons)."""
    cap = plan_cap(needed, policy.stop_threshold)
    rejected = {}
    ordered = sorted(options, key=lambda c: (-c.expected_loss, c.kind != "STK", c.con_id, -c.quantity))
    for c in ordered:
        label = f"{c.label}:{c.quantity}"
        if c.expected_loss > cap:
            rejected[label] = "Would plan past the median gap"
            continue
        if expense_remaining is not None and c.expected_loss > expense_remaining:
            rejected[label] = "Daily expense cap"
            continue
        improvement = l1_improvement(z, weights, c.expected_loss)
        if improvement <= policy.minimum_improvement:
            rejected[label] = "Modeled improvement below minimum"
            continue
        if risk_error is not None and (error := risk_error(c)):
            rejected[label] = "Temporary-inventory risk: " + error
            continue
        return c, improvement, rejected
    return None, 0.0, rejected

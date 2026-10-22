"""Exact vectorized twin of the app's scenario repricing (harness speed-up only; no model change).

hedge.valuation.ValuationEngine.price uses QuantLib's BjerksundStenslandApproximationEngine (QL 1.43:
the 1993 approximation) when there are no cash dividends; with flat curves its time change keeps
r*T, q*T and sigma^2*T, so the closed form below with T = remaining years is the same function.
`verify()` compares a full ScenarioEngine.build against the original to ~1e-9 dollars.
Only ScenarioEngine._reprice and ScenarioEngine.stress_matrix are replaced; everything else (market
stress rows, rhos, solver, risk) still runs through the app's own code.
"""

from __future__ import annotations

from datetime import datetime, time

import numpy as np
from scipy.special import log_ndtr, ndtr

from hedge.config import NY

YEAR = 365 * 86400


def _bsm(S, K, T, r, b, sig, call):
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (b + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if call:
        return S * np.exp((b - r) * T) * ndtr(d1) - K * np.exp(-r * T) * ndtr(d2)
    return K * np.exp(-r * T) * ndtr(-d2) - S * np.exp((b - r) * T) * ndtr(-d1)


def _log_phi_terms(S, T, g, H, I, r, b, sig):
    """log of the two (positive) parts of phi: exp(lam) S^g N(d) and exp(lam) S^g (I/S)^kap N(d2)."""
    lam = (-r + g * b + 0.5 * g * (g - 1) * sig * sig) * T
    sq = sig * np.sqrt(T)
    d = -(np.log(S / H) + (b + (g - 0.5) * sig * sig) * T) / sq
    kap = 2 * b / (sig * sig) + (2 * g - 1)
    base = lam + g * np.log(S)
    return base + log_ndtr(d), base + kap * np.log(I / S) + log_ndtr(d - 2 * np.log(I / S) / sq)


def _phi(S, T, g, H, I, r, b, sig, log_coef=0.0):
    """coef * phi, evaluated in logs (no overflow for large beta)."""
    a, c = _log_phi_terms(S, T, g, H, I, r, b, sig)
    return np.exp(log_coef + a) - np.exp(log_coef + c)


def _bs93_call(S, K, T, r, b, sig):
    """Bjerksund-Stensland (1993) American call; T, r, b scalars; S, K, sig arrays or scalars."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    sig = np.asarray(sig, dtype=float)
    if b >= r:
        return _bsm(S, K, T, r, b, sig, True)
    s2 = sig * sig
    beta = (0.5 - b / s2) + np.sqrt((b / s2 - 0.5) ** 2 + 2 * r / s2)
    binf = beta / (beta - 1) * K
    b0 = np.maximum(K, r / (r - b) * K)
    h = -(b * T + 2 * sig * np.sqrt(T)) * b0 / (binf - b0)
    I = b0 + (binf - b0) * (1 - np.exp(h))
    with np.errstate(all="ignore"):
        log_alpha = np.log(I - K) - beta * np.log(I)
        inside = (
            np.exp(log_alpha + beta * np.log(S))
            - _phi(S, T, beta, I, I, r, b, sig, log_alpha)
            + _phi(S, T, 1, I, I, r, b, sig)
            - _phi(S, T, 1, K, I, r, b, sig)
            - K * _phi(S, T, 0, I, I, r, b, sig)
            + K * _phi(S, T, 0, K, I, r, b, sig)
        )
    return np.where(S < I, inside, S - K)


def price_vec(manifest, c, spot, iv, at, rate=None):
    """Vectorized ValuationEngine.price (American, no cash dividends)."""
    spot = np.asarray(spot, dtype=float)
    iv = np.asarray(iv, dtype=float)
    if c.kind == "STK":
        return spot + 0 * iv
    if manifest.dividends.get(c.symbol):
        raise NotImplementedError("cash dividends use the FD engine")
    r = manifest.risk_free_rate if rate is None else rate
    q = manifest.dividend_yields.get(c.symbol, 0)
    expiration = datetime.combine(c.expiry, time(16), NY)
    T = (expiration - at.astimezone(NY)).total_seconds() / YEAR
    if T <= 0:
        payoff = np.maximum(spot - c.strike, 0) if c.right == "C" else np.maximum(c.strike - spot, 0)
        return payoff + 0 * iv
    b = r - q
    K = float(c.strike)
    # QuantLib 1.43 returns max(Bjerksund-Stensland 1993, European BSM).
    if c.right == "C":
        american = _bs93_call(spot, K, T, r, b, iv)
    else:
        american = _bs93_call(K, spot, T, r - b, -b, iv)
    return np.maximum(american, _bsm(spot, K, T, r, b, iv, c.right == "C"))


def make_reprice(original):
    def _reprice(self, ids, quotes, start, finish, shocks, iv_shocks, scale=1.0):
        n = len(next(iter(shocks.values())))
        changes = np.empty((n, len(ids)))
        for j, identifier in enumerate(ids):
            c, q = self.manifest.contract_map[identifier], quotes[identifier]
            shift = np.clip(np.asarray(shocks[c.symbol], dtype=float) * scale, -0.8, 2)
            if c.kind == "STK":
                changes[:, j] = c.multiplier * (q.spot * shift)
                continue
            if q.iv is None:
                raise ValueError(f"Missing implied volatility for {c.label}")
            current = self.valuation.price(c, q.spot, q.iv, start)
            iv = np.maximum(0.01, q.iv + np.asarray(iv_shocks[c.symbol], dtype=float) * scale)
            future = price_vec(self.manifest, c, q.spot * (1 + shift), iv, finish)
            changes[:, j] = c.multiplier * (future - current)
        return changes

    return _reprice


def make_stress(original):
    def stress_matrix(self, ids, quotes, now, end):
        symbols = sorted({self.manifest.contract_map[i].symbol for i in ids})
        shifts = np.array(
            [
                (r, v, skew)
                for r in (-0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2)
                for v in (-0.15, -0.05, 0, 0.05, 0.15)
                for skew in (-0.1, 0.1)
            ]
        )
        shifted, unshifted = {}, {}
        for i in ids:
            c, q = self.manifest.contract_map[i], quotes[i]
            if c.kind == "STK":
                shifted[i] = c.multiplier * q.spot * shifts[:, 0]
                unshifted[i] = 0.0
                continue
            if q.iv is None:
                raise ValueError(f"Missing implied volatility for {c.label}")
            current = self.valuation.price(c, q.spot, q.iv, now)
            moneyness = c.strike / q.spot - 1
            iv = np.maximum(0.01, q.iv + shifts[:, 1] + shifts[:, 2] * moneyness)
            shifted[i] = c.multiplier * (price_vec(self.manifest, c, q.spot * (1 + shifts[:, 0]), iv, end) - current)
            iv0 = max(0.01, q.iv + 0 + 0 * moneyness)
            unshifted[i] = c.multiplier * (float(price_vec(self.manifest, c, q.spot * (1 + 0), iv0, end)) - current)
        rows = []
        for affected in [None, *symbols] if len(symbols) > 1 else [None]:
            block = np.empty((len(shifts), len(ids)))
            for j, i in enumerate(ids):
                applies = affected is None or self.manifest.contract_map[i].symbol == affected
                block[:, j] = shifted[i] if applies else unshifted[i]
            rows.append(block)
        rate_rows = []
        for rate_shift in (-self.manifest.risk.rate_stress_shift, self.manifest.risk.rate_stress_shift):
            row = []
            for i in ids:
                c, q = self.manifest.contract_map[i], quotes[i]
                if c.kind == "STK":
                    row.append(0.0)
                    continue
                current = self.valuation.price(c, q.spot, q.iv, now)
                future = float(
                    price_vec(self.manifest, c, q.spot, max(0.01, q.iv), end,
                              rate=self.manifest.risk_free_rate + rate_shift)
                )
                row.append(c.multiplier * (future - current))
            rate_rows.append(row)
        return np.vstack([*rows, np.array(rate_rows)])

    return stress_matrix


def install():
    from hedge.scenarios import ScenarioEngine

    if getattr(ScenarioEngine, "_fastval", False):
        return
    ScenarioEngine._orig_reprice = ScenarioEngine._reprice
    ScenarioEngine._orig_stress = ScenarioEngine.stress_matrix
    ScenarioEngine._reprice = make_reprice(ScenarioEngine._reprice)
    ScenarioEngine.stress_matrix = make_stress(ScenarioEngine.stress_matrix)
    ScenarioEngine._fastval = True


def verify(engine, snapshot, quotes, now, ids):
    """Max |difference| between the vectorized and original builds (dollars)."""
    from hedge.scenarios import ScenarioEngine

    fast = engine.build(snapshot, quotes, now, ids)
    saved = ScenarioEngine._reprice, ScenarioEngine.stress_matrix
    ScenarioEngine._reprice, ScenarioEngine.stress_matrix = ScenarioEngine._orig_reprice, ScenarioEngine._orig_stress
    try:
        slow = engine.build(snapshot, quotes, now, ids)
    finally:
        ScenarioEngine._reprice, ScenarioEngine.stress_matrix = saved
    diffs = {
        "changes": float(np.max(np.abs(fast.changes - slow.changes))),
        "stress": float(np.max(np.abs(fast.stress - slow.stress))),
        "next": float(np.max(np.abs(fast.next_changes - slow.next_changes))) if slow.next_changes is not None else 0.0,
        "overnight": float(np.max(np.abs(fast.overnight_changes - slow.overnight_changes)))
        if slow.overnight_changes is not None else 0.0,
        "identifier_equal": fast.identifier == slow.identifier,
    }
    return diffs

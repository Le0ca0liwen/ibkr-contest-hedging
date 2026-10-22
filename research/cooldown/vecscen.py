"""Vectorized drop-ins for ScenarioEngine._reprice / stress_matrix (same math, numpy BjS twin of QL 1.43).

Verified against the originals by probe/vecscen_check.py. Falls back to the originals when a symbol
has cash dividends (the FD engine path).
"""
from datetime import datetime, time

import numpy as np

import hedge.scenarios as S
import vecbs
from hedge.config import NY

_orig_reprice = S.ScenarioEngine._reprice
_orig_stress = S.ScenarioEngine.stress_matrix


def _vec_price(manifest, c, spots, ivs, at, rates):
    expiration = datetime.combine(c.expiry, time(16), NY)
    remaining = (expiration - at.astimezone(NY)).total_seconds() / (365 * 86400)
    spots = np.asarray(spots, dtype=float)
    if remaining <= 0:
        return np.maximum(spots - c.strike, 0) if c.right == "C" else np.maximum(c.strike - spots, 0)
    q = manifest.dividend_yields.get(c.symbol, 0)
    return vecbs.american(spots, c.strike, c.right == "C", rates, q, ivs, remaining)


def _has_dividends(self, ids):
    return any(self.manifest.dividends.get(self.manifest.contract_map[i].symbol) for i in ids)


def _changes(self, c, q, now, end, ret, ivs, skew, rate_shift, current):
    """Vector of ValuationEngine.scenario_change (no multiplier)."""
    if c.kind == "STK":
        return q.spot * np.asarray(ret, dtype=float)
    if q.iv is None:
        raise ValueError(f"Missing implied volatility for {c.label}")
    if current is None:
        current = self.valuation.price(c, q.spot, q.iv, now)
    moneyness = c.strike / q.spot - 1
    future = _vec_price(self.manifest, c, q.spot * (1 + np.asarray(ret, dtype=float)),
                        np.maximum(0.01, q.iv + np.asarray(ivs, dtype=float) + np.asarray(skew, dtype=float) * moneyness),
                        end, self.manifest.risk_free_rate + np.asarray(rate_shift, dtype=float))
    return future - current


def _batched(self, items, now_by_item, end):
    """items: list of (key, c, q, ret[], ivs[], skew[], rate_shift[], current) -> {key: change vector (no mult)}.

    Groups options by (expiry, right, dividend yield) and prices each group with one vecbs call.
    """
    out, groups = {}, {}
    for key, c, q, ret, ivs, skew, rshift, current in items:
        if c.kind == "STK":
            out[key] = q.spot * np.asarray(ret, dtype=float)
            continue
        if q.iv is None:
            raise ValueError(f"Missing implied volatility for {c.label}")
        if current is None:
            current = self.valuation.price(c, q.spot, q.iv, now_by_item)
        moneyness = c.strike / q.spot - 1
        n = len(ret)
        spots = q.spot * (1 + np.asarray(ret, dtype=float))
        vols = np.maximum(0.01, q.iv + np.asarray(ivs, dtype=float) + np.asarray(skew, dtype=float) * moneyness)
        rates = self.manifest.risk_free_rate + np.broadcast_to(np.asarray(rshift, dtype=float), (n,))
        g = groups.setdefault((c.expiry, c.right, self.manifest.dividend_yields.get(c.symbol, 0)), [])
        g.append((key, c, spots, vols, rates, np.full(n, c.strike), current))
    for (expiry, right, q_yield), members in groups.items():
        c0 = members[0][1]
        expiration = datetime.combine(expiry, time(16), NY)
        remaining = (expiration - end.astimezone(NY)).total_seconds() / (365 * 86400)
        spots = np.concatenate([m[2] for m in members])
        strikes = np.concatenate([m[5] for m in members])
        if remaining <= 0:
            prices = np.maximum(spots - strikes, 0) if right == "C" else np.maximum(strikes - spots, 0)
        else:
            prices = vecbs.american(spots, strikes, right == "C", np.concatenate([m[4] for m in members]), q_yield,
                                    np.concatenate([m[3] for m in members]), remaining)
        offset = 0
        for key, c, sp, _, _, _, current in members:
            out[key] = prices[offset:offset + len(sp)] - current
            offset += len(sp)
        del c0
    return out


def reprice(self, ids, quotes, start, finish, shocks, iv_shocks, scale=1.0):
    if _has_dividends(self, ids):
        return _orig_reprice(self, ids, quotes, start, finish, shocks, iv_shocks, scale)
    n = len(next(iter(shocks.values())))
    items = []
    for identifier in ids:
        c, q = self.manifest.contract_map[identifier], quotes[identifier]
        current = self.valuation.price(c, q.spot, q.iv, start) if c.kind == "OPT" and q.iv else None
        ret = np.clip(np.asarray(shocks[c.symbol], dtype=float) * scale, -0.8, 2)
        ivs = np.asarray(iv_shocks[c.symbol], dtype=float) * scale
        items.append((identifier, c, q, ret, ivs, np.zeros(n), np.zeros(n), current))
    values = _batched(self, items, start, finish)
    changes = np.empty((n, len(ids)))
    for j, identifier in enumerate(ids):
        changes[:, j] = self.manifest.contract_map[identifier].multiplier * values[identifier]
    return changes


def stress_matrix(self, ids, quotes, now, end):
    if _has_dividends(self, ids):
        return _orig_stress(self, ids, quotes, now, end)
    symbols = sorted({self.manifest.contract_map[i].symbol for i in ids})
    shifts = [(r, v, skew) for r in (-0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2) for v in (-0.15, -0.05, 0, 0.05, 0.15)
              for skew in (-0.1, 0.1)]
    keys = [(0, 0, 0), *shifts]
    arr = np.array(keys, dtype=float)
    rate = self.manifest.risk.rate_stress_shift
    items = []
    for i in ids:
        c, q = self.manifest.contract_map[i], quotes[i]
        current = self.valuation.price(c, q.spot, q.iv, now) if c.kind == "OPT" and q.iv else None
        items.append((("grid", i), c, q, arr[:, 0], arr[:, 1], arr[:, 2], np.zeros(len(keys)), current))
        items.append((("rate", i), c, q, np.zeros(2), np.zeros(2), np.zeros(2), np.array([-rate, rate]), current))
    values = _batched(self, items, now, end)
    table = {i: dict(zip(keys, self.manifest.contract_map[i].multiplier * values[("grid", i)], strict=True))
             for i in ids}
    rows = []
    for affected in [None, *symbols] if len(symbols) > 1 else [None]:
        for shift in shifts:
            rows.append([table[i][shift if affected is None or self.manifest.contract_map[i].symbol == affected
                                  else (0, 0, 0)] for i in ids])
    for k in range(2):
        rows.append([self.manifest.contract_map[i].multiplier * float(values[("rate", i)][k]) for i in ids])
    return np.array(rows)


def install():
    S.ScenarioEngine._reprice = reprice
    S.ScenarioEngine.stress_matrix = stress_matrix

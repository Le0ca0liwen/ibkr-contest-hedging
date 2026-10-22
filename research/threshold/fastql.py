"""Bit-identical, faster drop-in for hedge.valuation.ValuationEngine.price (sim-only patch).

Same QuantLib engines and inputs; only the QL objects are reused (SimpleQuote-driven term
structures) instead of being rebuilt on every call. Falls back to the original for any
contract with scheduled cash dividends. install() verifies exact equality before patching.
"""

import math
from datetime import datetime, time

import QuantLib as ql

import hedge.valuation as val
from hedge.config import NY

_ORIGINAL = val.ValuationEngine.price
_START = ql.Date(1, 1, 2020)
_END = _START + 3650
_REPRESENTED = ql.Actual365Fixed().yearFraction(_START, _END)
_CACHE = {}


def _entry(strike, right, european):
    key = (strike, right, european)
    entry = _CACHE.get(key)
    if entry is None:
        day_count = ql.Actual365Fixed()
        spot_q, rate_q, div_q, vol_q = ql.SimpleQuote(1.0), ql.SimpleQuote(0.0), ql.SimpleQuote(0.0), ql.SimpleQuote(0.2)
        rate = ql.YieldTermStructureHandle(ql.FlatForward(_START, ql.QuoteHandle(rate_q), day_count))
        dividends = ql.YieldTermStructureHandle(ql.FlatForward(_START, ql.QuoteHandle(div_q), day_count))
        vol = ql.BlackVolTermStructureHandle(
            ql.BlackConstantVol(_START, ql.NullCalendar(), ql.QuoteHandle(vol_q), day_count)
        )
        process = ql.BlackScholesMertonProcess(ql.QuoteHandle(spot_q), dividends, rate, vol)
        exercise = ql.EuropeanExercise(_END) if european else ql.AmericanExercise(_START, _END)
        option = ql.VanillaOption(
            ql.PlainVanillaPayoff(ql.Option.Call if right == "C" else ql.Option.Put, strike), exercise
        )
        engine = (
            ql.AnalyticEuropeanEngine(process) if european else ql.BjerksundStenslandApproximationEngine(process)
        )
        option.setPricingEngine(engine)
        entry = (spot_q, rate_q, div_q, vol_q, option, process, engine)
        _CACHE[key] = entry
    return entry


def fast_price(self, c, spot, iv, at, european=False, *, risk_free_rate=None):
    if c.kind == "STK":
        return spot
    if spot <= 0 or iv <= 0 or at.tzinfo is None:
        raise ValueError("Positive spot/IV and timezone-aware valuation time required")
    annual_rate = self.manifest.risk_free_rate if risk_free_rate is None else risk_free_rate
    if not math.isfinite(annual_rate):
        raise ValueError("Finite risk-free rate required")
    if self.manifest.dividends.get(c.symbol):
        return _ORIGINAL(self, c, spot, iv, at, european, risk_free_rate=risk_free_rate)
    expiration = datetime.combine(c.expiry, time(16), NY)
    remaining = (expiration - at.astimezone(NY)).total_seconds() / (365 * 86400)
    payoff = max(spot - c.strike, 0) if c.right == "C" else max(c.strike - spot, 0)
    if remaining <= 0:
        return payoff
    with val._QL_LOCK:
        settings = ql.Settings.instance()
        original = settings.evaluationDate
        ratio = remaining / _REPRESENTED
        try:
            settings.evaluationDate = _START
            spot_q, rate_q, div_q, vol_q, option, _, _ = _entry(c.strike, c.right, european)
            spot_q.setValue(spot)
            rate_q.setValue(annual_rate * ratio)
            div_q.setValue(self.manifest.dividend_yields.get(c.symbol, 0) * ratio)
            vol_q.setValue(iv * math.sqrt(ratio))
            return float(option.NPV())
        finally:
            settings.evaluationDate = original


def verify(manifest, n=20000, seed=3):
    import numpy as np
    from datetime import timedelta

    engine = val.ValuationEngine(manifest)
    rng = np.random.default_rng(seed)
    options = [c for c in manifest.contracts if c.kind == "OPT"]
    base = datetime(2026, 10, 22, 9, 30, tzinfo=NY)
    mismatches = 0
    worst = 0.0
    for k in range(n):
        c = options[k % len(options)]
        spot = float(c.strike * np.exp(rng.normal(0, 0.15)))
        iv = float(max(0.01, rng.uniform(0.005, 1.5)))
        at = base + timedelta(seconds=float(rng.uniform(0, 3 * 86400)))
        european = bool(rng.random() < 0.2)
        rfr = None if rng.random() < 0.7 else float(0.04 + rng.normal(0, 0.01))
        a = _ORIGINAL(engine, c, spot, iv, at, european, risk_free_rate=rfr)
        b = fast_price(engine, c, spot, iv, at, european, risk_free_rate=rfr)
        if a != b:
            mismatches += 1
            worst = max(worst, abs(a - b))
    return mismatches, worst


def install(manifest, n=5000):
    mismatches, worst = verify(manifest, n)
    if mismatches:
        raise RuntimeError(f"fast pricer not bit-identical: {mismatches} mismatches, worst {worst}")
    val.ValuationEngine.price = fast_price
    return n

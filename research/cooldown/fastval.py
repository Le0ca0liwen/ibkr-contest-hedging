"""Exact speed-up of hedge.valuation.ValuationEngine.price for the no-dividend case.

Same QuantLib engine (Bjerksund-Stensland / analytic European), same time-change trick and inputs;
only the QL object graph is built once per contract and its SimpleQuotes are updated per call.
Falls back to the original implementation for dividends or anything unusual.
"""
import math
import threading
from datetime import datetime, time

import QuantLib as ql

import hedge.valuation as V
from hedge.config import NY

_orig_price = V.ValuationEngine.price
_LOCK = V._QL_LOCK
_START = ql.Date(1, 1, 2020)
_END = _START + 3650
_DC = ql.Actual365Fixed()
_REPRESENTED = _DC.yearFraction(_START, _END)
_cache = {}


def _setup(c, european):
    key = (c.strike, c.right, european)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    spot, rate, div, vol = ql.SimpleQuote(100.0), ql.SimpleQuote(0.0), ql.SimpleQuote(0.0), ql.SimpleQuote(0.2)
    rts = ql.YieldTermStructureHandle(ql.FlatForward(_START, ql.QuoteHandle(rate), _DC))
    dts = ql.YieldTermStructureHandle(ql.FlatForward(_START, ql.QuoteHandle(div), _DC))
    vts = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(_START, ql.NullCalendar(), ql.QuoteHandle(vol), _DC))
    process = ql.BlackScholesMertonProcess(ql.QuoteHandle(spot), dts, rts, vts)
    exercise = ql.EuropeanExercise(_END) if european else ql.AmericanExercise(_START, _END)
    option = ql.VanillaOption(ql.PlainVanillaPayoff(ql.Option.Call if c.right == "C" else ql.Option.Put, c.strike),
                              exercise)
    option.setPricingEngine(ql.AnalyticEuropeanEngine(process) if european
                            else ql.BjerksundStenslandApproximationEngine(process))
    hit = (option, spot, rate, div, vol)
    _cache[key] = hit
    return hit


def fast_price(self, c, spot, iv, at, european=False, *, risk_free_rate=None):
    if c.kind == "STK":
        return spot
    if self.manifest.dividends.get(c.symbol):
        return _orig_price(self, c, spot, iv, at, european, risk_free_rate=risk_free_rate)
    if spot <= 0 or iv <= 0 or at.tzinfo is None:
        raise ValueError("Positive spot/IV and timezone-aware valuation time required")
    annual_rate = self.manifest.risk_free_rate if risk_free_rate is None else risk_free_rate
    if not math.isfinite(annual_rate):
        raise ValueError("Finite risk-free rate required")
    expiration = datetime.combine(c.expiry, time(16), NY)
    remaining = (expiration - at.astimezone(NY)).total_seconds() / (365 * 86400)
    payoff = max(spot - c.strike, 0) if c.right == "C" else max(c.strike - spot, 0)
    if remaining <= 0:
        return payoff
    ratio = remaining / _REPRESENTED
    yield_rate = self.manifest.dividend_yields.get(c.symbol, 0)
    with _LOCK:
        settings = ql.Settings.instance()
        if settings.evaluationDate != _START:
            # Every QL use in the app goes through ValuationEngine.price, which sets this date itself.
            settings.evaluationDate = _START
        option, sq, rq, dq, vq = _setup(c, european)
        for q, v in ((sq, spot), (rq, annual_rate * ratio), (dq, yield_rate * ratio), (vq, iv * math.sqrt(ratio))):
            if q.value() != v:
                q.setValue(v)
        return float(option.NPV())


def install():
    V.ValuationEngine.price = fast_price

"""Optional helper for simulators: real 1-minute paths -> per-second spots, and per-contract IV/spread.

    import sys; sys.path.insert(0, "<RESEARCH_ROOT>/marketdata")
    from replay_loader import sessions, spot_path, option_inputs, quote_from_price
    t0, paths = spot_path("2026-10-20")                       # absolute real prices
    t0, paths = spot_path("2026-10-15", start_spot={"SPCX": 160.30, "RKLB": 53.24, "SPY": 773.85})
    paths["SPCX"][k] = spot k seconds after 09:30:00 ET (k = 0 .. 23400)
    inp = option_inputs()   # {(symbol, 'YYYYMMDD', strike, right): {"iv": .., "rel_spread": .., ...}}

Within-minute model: the bar's open is pinned at the start of the minute and its close at the end
(linear in between); bridge=True adds a seeded Brownian bridge whose scale matches the bar's
high-low range (Parkinson), so intraminute wiggles exist but every knot is the real price.
With start_spot, the session is replayed as RELATIVE moves (close / session open) from that spot.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, time

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
NY = ZoneInfo("America/New_York")
SYMS = ("SPCX", "RKLB", "SPY")
_BARS = None


def _bars():
    global _BARS
    if _BARS is None:
        b = pd.read_csv(os.path.join(HERE, "bars_1m.csv"))
        b["timestamp_et"] = pd.to_datetime(b.timestamp_et, utc=True).dt.tz_convert(NY)
        _BARS = b
    return _BARS


def sessions(complete_only=True):
    s = pd.read_csv(os.path.join(HERE, "sessions_summary.csv"))
    if complete_only:
        s = s[s.official_close.notna()]
    return sorted(s.date.unique())


def official_close(date_str):
    s = pd.read_csv(os.path.join(HERE, "sessions_summary.csv"))
    s = s[s.date == date_str]
    return {r.symbol: r.official_close for r in s.itertuples()}


def spot_path(date_str, start_spot=None, bridge=False, seed=0):
    """Per-second spot arrays (len 23401: 09:30:00 .. 16:00:00 ET) for one session."""
    b = _bars()
    b = b[b.date == date_str]
    t0 = datetime.combine(pd.Timestamp(date_str).date(), time(9, 30), NY)
    rng = np.random.default_rng(seed)
    out = {}
    for s in SYMS:
        g = b[b.symbol == s].sort_values("timestamp_et")
        mins = ((g.timestamp_et - t0).dt.total_seconds() // 60).astype(int).to_numpy()
        knots_t, knots_p = [], []
        for m, o, c in zip(mins, g.open.to_numpy(), g.close.to_numpy(), strict=True):
            knots_t += [m * 60, m * 60 + 59.999]
            knots_p += [o, c]
        sec = np.arange(0, 23401)
        last = knots_t[-1]
        p = np.interp(np.minimum(sec, last), knots_t, knots_p)
        if bridge:
            for m, h, lo in zip(mins, g.high.to_numpy(), g.low.to_numpy(), strict=True):
                sig = math.log(h / lo) / math.sqrt(4 * math.log(2)) if h > lo else 0.0  # Parkinson, per minute
                if sig <= 0:
                    continue
                w = np.concatenate([[0.0], np.cumsum(rng.standard_normal(59))]) * sig / math.sqrt(60)
                w = w - np.arange(60) / 59 * w[-1]  # pin both ends (open at :00, close at :59)
                i = slice(m * 60, m * 60 + 60)
                p[i] = p[i] * np.exp(w[: len(p[i])])
        if start_spot is not None:
            p = start_spot[s] * p / g.open.iloc[0]
        out[s] = p
    return t0, out


def option_inputs():
    c = pd.read_csv(os.path.join(HERE, "options_contracts.csv"))
    res = {}
    for r in c.itertuples():
        res[(r.symbol, str(r.expiry), float(r.strike), r.right)] = dict(
            iv=r.iv_sim, rel_spread=r.rel_spread_sim, iv_source=r.iv_sim_source, spread_source=r.rel_spread_sim_source,
            role=r.role, mandated_qty=r.mandated_qty, in_app_hedge_universe=r.in_app_hedge_universe,
            contest_tradable=r.contest_tradable, chain_rel_spread=r.chain_rel_spread, today_rel_spread=r.today_rel_spread)
    return res


def quote_from_price(price, rel_spread, tick=0.01, min_spread=0.01):
    """Symmetric bid/ask around a model price with the contract's real relative spread."""
    half = max(min_spread / 2, 0.5 * rel_spread * price)
    bid = max(tick, math.floor((price - half) / tick + 1e-9) * tick)
    ask = math.ceil((price + half) / tick - 1e-9) * tick
    return round(bid, 4), round(ask, 4)

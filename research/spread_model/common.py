"""Shared inputs for the spread-model lane: manifest, real-path replay market, per-session caches.

Replay convention (identical for every variant):
  * every real session D (2026-10-08 .. 10-20) is replayed as contest day 2026-10-22 (first scored
    session; previous close = Day 0 = 2026-10-21 16:00), so option time-to-expiry is the same;
  * spots are RELATIVE to the previous official close, anchored at today's 13:13 spots:
      S_t = S_today * raw_t / official_close(D-1)   (includes D's real overnight gap)
  * options are priced with the app's ValuationEngine at the per-contract iv_sim (sticky strike),
    quoted symmetrically around the model price with the contract's real relative spread
    (rel_spread_sim: today's 13:13 spread when quoted, else the Oct-20 chain's);
  * stocks: SPCX/RKLB 2-cent and SPY 1-cent spreads around the real 1-minute path.
"""

from __future__ import annotations

import copy
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# research/ root (env RESEARCH_ROOT, default: the folder above this script) and the app source (env REPO,
# default: the repository root above research/; replay_sim.py --repo sets REPO before importing this).
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
LANE = RESEARCH_ROOT / "spread_model"
DATA = RESEARCH_ROOT / "marketdata"
CACHE = LANE / "cache"
REPO = os.environ.get("REPO") or str(RESEARCH_ROOT.parent)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from hedge.config import NY, Manifest  # noqa: E402
from hedge.valuation import ValuationEngine  # noqa: E402

sys.path.insert(0, str(DATA))
import replay_loader as RL  # noqa: E402

TODAY_SPOT = {"SPCX": 160.30, "RKLB": 53.24, "SPY": 773.85}
STOCK_SPREAD = {"SPCX": 0.02, "RKLB": 0.02, "SPY": 0.01}
STOCK_SIZE = 500
OPTION_SIZE = 50
CONTEST_DAY = date(2026, 10, 22)
DAY_ZERO = date(2026, 10, 21)
OPEN = datetime.combine(CONTEST_DAY, time(9, 30), NY)
CLOSE = datetime.combine(CONTEST_DAY, time(16, 0), NY)
PREV_CLOSE_AT = datetime.combine(DAY_ZERO, time(16, 0), NY)
SECONDS = 23400
SESSIONS = [
    "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14",
    "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
]
SPY_EXPIRY_PATCH = {1014: "2026-11-13", 1015: "2026-11-13", 1016: "2026-11-27", 1017: "2026-11-27"}


def history_file():
    """Real WSJ daily closes through 2026-10-21 (partial row included) for the scenario covariance."""
    path = LANE / "daily_history.csv"
    if not path.exists():
        d = pd.read_csv(DATA / "daily_1y.csv")
        d = d[d.date <= "2026-10-21"][["date", "symbol", "close"]]
        d.to_csv(path, index=False)
    return str(path)


def manifest_dict(policy=None, execution=None, risk=None):
    data = yaml.safe_load((RESEARCH_ROOT / "config" / "small.contest.yaml").read_text())
    for c in data["contracts"]:
        if c["con_id"] in SPY_EXPIRY_PATCH:
            c["expiry"] = SPY_EXPIRY_PATCH[c["con_id"]]
    data["daily_stock_history"] = history_file()
    data["policy"] = {**data["policy"], "burn_surplus": False, "overnight_weight": 0.5, **(policy or {})}
    data["execution"] = {**data["execution"], **(execution or {})}
    data["risk"] = {**data["risk"], **(risk or {})}
    data["burner"] = {"mode": "off"}
    data["autostart"] = "paper"
    return data


def build_manifest(policy=None, execution=None, risk=None, extra=None):
    data = manifest_dict(policy, execution, risk)
    if extra:
        data.update(copy.deepcopy(extra))
    return Manifest.model_validate(data)


def option_key(c):
    return (c.symbol, c.expiry.strftime("%Y%m%d"), float(c.strike), c.right)


def prev_close(day):
    s = pd.read_csv(DATA / "sessions_summary.csv")
    days = sorted(s.date.unique())
    i = days.index(day)
    if i > 0:
        prev = days[i - 1]
        return {r.symbol: r.official_close for r in s[s.date == prev].itertuples()}
    d = pd.read_csv(DATA / "daily_1y.csv")
    d = d[d.date < day]
    last = d.date.max()
    return {r.symbol: r.close for r in d[d.date == last].itertuples()}


def build_cache(day, manifest=None):
    """Per-second spots and option model prices; greeks every 5 s.  Shared by every variant."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{day}.npz"
    if path.exists():
        return path
    manifest = manifest or build_manifest()
    engine = ValuationEngine(manifest)
    _, raw = RL.spot_path(day)
    pc = prev_close(day)
    official = RL.official_close(day)
    spots = {s: TODAY_SPOT[s] * raw[s] / pc[s] for s in TODAY_SPOT}
    close_spot = {s: TODAY_SPOT[s] * official[s] / pc[s] for s in TODAY_SPOT}
    inputs = RL.option_inputs()
    out = {f"spot_{s}": spots[s] for s in spots}
    out.update({f"close_{s}": np.array(close_spot[s]) for s in close_spot})
    out.update({f"open_{s}": np.array(spots[s][0]) for s in spots})
    for c in manifest.contracts:
        if c.kind != "OPT":
            continue
        meta = inputs[option_key(c)]
        iv = float(meta["iv"])
        S = spots[c.symbol]
        price = np.empty(SECONDS + 1)
        for k in range(SECONDS + 1):
            price[k] = engine.price(c, float(S[k]), iv, OPEN + timedelta(seconds=k))
        g = np.empty((SECONDS // 5 + 1, 5))
        for j, k in enumerate(range(0, SECONDS + 1, 5)):
            gr = engine.greeks(c, float(S[k]), iv, OPEN + timedelta(seconds=k))
            g[j] = [gr["delta"], gr["gamma"], gr["vega"], gr["theta"], gr["rho"]]
        out[f"px_{c.con_id}"] = price
        out[f"gk_{c.con_id}"] = g
        out[f"iv_{c.con_id}"] = np.array(iv)
        out[f"rs_{c.con_id}"] = np.array(float(meta["rel_spread"]))
        out[f"closepx_{c.con_id}"] = np.array(engine.price(c, close_spot[c.symbol], iv, CLOSE))
        out[f"prevpx_{c.con_id}"] = np.array(engine.price(c, TODAY_SPOT[c.symbol], iv, PREV_CLOSE_AT))
    np.savez_compressed(path, **out)
    return path


def quote_from_price(price, rel_spread, tick=0.01, min_spread=0.01):
    half = max(min_spread / 2, 0.5 * rel_spread * price)
    bid = max(tick, math.floor((price - half) / tick + 1e-9) * tick)
    ask = math.ceil((price + half) / tick - 1e-9) * tick
    return round(bid, 4), round(ask, 4)


class ReplayMarket:
    """Quotes at sim time `now` from the per-session cache (Quote objects the app consumes)."""

    def __init__(self, manifest, day, spread_scale=1.0):
        from hedge.models import Quote

        self.Quote = Quote
        self.m = manifest
        self.day = day
        z = np.load(build_cache(day, manifest))
        self.z = {k: z[k] for k in z.files}
        self.spread_scale = spread_scale
        self.opts = [c for c in manifest.contracts if c.kind == "OPT"]
        self.stks = [c for c in manifest.contracts if c.kind == "STK"]

    def index(self, now):
        return int(min(max((now - OPEN).total_seconds(), 0), SECONDS))

    def spot(self, symbol, now):
        return float(self.z[f"spot_{symbol}"][self.index(now)])

    def stock_quote_prices(self, symbol, s):
        spread = STOCK_SPREAD[symbol]
        bid = math.floor((s - spread / 2) * 100 + 1e-9) / 100
        return bid, round(bid + spread, 2)

    def option_prices(self, c, k):
        price = float(self.z[f"px_{c.con_id}"][k])
        bid, ask = quote_from_price(price, float(self.z[f"rs_{c.con_id}"]) * self.spread_scale)
        return price, bid, ask

    def quotes(self, now):
        k = self.index(now)
        out, mids = {}, {}
        for c in self.stks:
            s = float(self.z[f"spot_{c.symbol}"][k])
            bid, ask = self.stock_quote_prices(c.symbol, s)
            mids[c.symbol] = (bid + ask) / 2
            out[c.con_id] = self.Quote(
                provider="synthetic", con_id=c.con_id, bid=bid, ask=ask, bid_size=STOCK_SIZE,
                ask_size=STOCK_SIZE, observed_at=now, data_type="synthetic", spot=mids[c.symbol],
                model_price=mids[c.symbol], shortable_shares=None,
            )
        for c in self.opts:
            price, bid, ask = self.option_prices(c, k)
            g = self.z[f"gk_{c.con_id}"][min(k // 5, len(self.z[f"gk_{c.con_id}"]) - 1)]
            out[c.con_id] = self.Quote(
                provider="synthetic", con_id=c.con_id, bid=bid, ask=ask, bid_size=OPTION_SIZE,
                ask_size=OPTION_SIZE, observed_at=now, data_type="synthetic", spot=mids[c.symbol],
                iv=float(self.z[f"iv_{c.con_id}"]), greek_at=now, model_price=price,
                delta=float(g[0]), gamma=float(g[1]), vega=float(g[2]), theta=float(g[3]), rho=float(g[4]),
            )
        return out

    def close_marks(self):
        """Official-close marks: stocks at the official close, options at the model price there."""
        marks = {}
        for c in self.stks:
            marks[c.con_id] = float(self.z[f"close_{c.symbol}"])
        for c in self.opts:
            marks[c.con_id] = float(self.z[f"closepx_{c.con_id}"])
        return marks

    def prev_marks(self):
        marks = {c.con_id: TODAY_SPOT[c.symbol] for c in self.stks}
        marks.update({c.con_id: float(self.z[f"prevpx_{c.con_id}"]) for c in self.opts})
        return marks

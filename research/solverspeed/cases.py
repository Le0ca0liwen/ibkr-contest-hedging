"""Contest-shaped solver-profiling cases (no broker, no network).

Mandated SPCX/RKLB/SPY book held; optimizer candidates = SPCX/RKLB shares + nearby SPCX/RKLB/SPY
options (other expiries/strikes) from the Oct 20 moomoo chain (chains.csv), quoted at today's spots
(SPCX 160.30, RKLB 53.24, SPY 773.85) with each strike's chain IV, priced at the clock.

Never contains forbidden contracts as candidates: no SPY shares, no Nov20 RKLB calls, no other
Nov20 SPY options. The candidate lists nest (12 c 14 c ... c 38) and every candidate survives
hedge.universe.screen_options, so candidate_count == requested count.

Quotes are cached per clock in cases/quotes_<HHMM>.json (generated once with the source on
sys.path), so every HEDGE_SRC variant is compared on identical inputs.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
LANE = RESEARCH_ROOT / "solverspeed"
CASES = LANE / "cases"
CHAINS = RESEARCH_ROOT / "marketdata" / "econ" / "chains.csv"
DAILY = RESEARCH_ROOT / "marketdata" / "econ" / "daily.csv"
BASE_YAML = RESEARCH_ROOT / "config" / "small.contest.yaml"

SPOT = {"SPCX": 160.30, "RKLB": 53.24, "SPY": 773.85}
STOCK_ID = {"SPCX": 10, "RKLB": 20, "SPY": 30}
N20, N13, N27 = date(2026, 11, 20), date(2026, 11, 13), date(2026, 11, 27)
SESSION = date(2026, 10, 22)

MANDATED = [  # con_id, symbol, strike, right, qty  (same ids as small.contest.yaml)
    (1001, "SPCX", 130.0, "C", 25), (1002, "SPCX", 145.0, "C", -50), (1003, "SPCX", 160.0, "C", 25),
    (1004, "RKLB", 45.0, "P", -25), (1005, "RKLB", 55.0, "P", 25),
    (1006, "SPY", 770.0, "C", 25), (1007, "SPY", 770.0, "P", 25),
]

# Per-symbol candidate options in priority order (first 3/3/4 = the composition of the Oct 20
# 12-candidate manifest). The full 12+12+12 is exactly what screen_options keeps from a full chain:
# SPCX 8 nearest Nov20 + 4 nearest Nov27; RKLB 8 nearest Nov20 puts + 4 Nov27; SPY (Nov20 forbidden)
# 6 nearest of each neighbouring expiry (Nov27, Nov13 in the Oct 20 chain).
PRIORITY = {
    "SPCX": [(N20, 160, "P"), (N27, 160, "C"), (N27, 160, "P"), (N20, 162.5, "C"), (N20, 162.5, "P"),
             (N27, 165, "C"), (N27, 165, "P"), (N20, 157.5, "C"), (N20, 157.5, "P"), (N20, 165, "C"),
             (N20, 165, "P"), (N20, 155, "C")],
    "RKLB": [(N20, 53, "P"), (N27, 53, "C"), (N27, 53, "P"), (N20, 54, "P"), (N27, 54, "C"),
             (N27, 54, "P"), (N20, 52, "P"), (N20, 51, "P"), (N20, 56, "P"), (N20, 50, "P"),
             (N20, 57, "P"), (N20, 49, "P")],
    "SPY": [(N13, 774, "C"), (N13, 774, "P"), (N27, 774, "C"), (N27, 774, "P"), (N13, 773, "C"),
            (N13, 773, "P"), (N27, 773, "C"), (N27, 773, "P"), (N13, 775, "C"), (N13, 775, "P"),
            (N27, 775, "C"), (N27, 775, "P")],
}
# candidates = 2 stocks + options; options per symbol (SPCX, RKLB, SPY)
SPLIT = {12: (3, 3, 4), 14: (4, 4, 4), 17: (5, 5, 5), 20: (6, 6, 6), 26: (8, 8, 8), 38: (12, 12, 12)}


def option_ids():
    """Stable con_ids 2001.. for every candidate option (nesting subsets keep their ids)."""
    out, k = {}, 2001
    for symbol in ("SPCX", "RKLB", "SPY"):
        for expiry, strike, right in PRIORITY[symbol]:
            out[(symbol, expiry, float(strike), right)] = k
            k += 1
    return out


def chain_rows():
    c = pd.read_csv(CHAINS)
    c = c[c.root.isin(["SPCX", "RKLB", "SPY"])].copy()
    c["exp"] = pd.to_datetime(c.expiry.astype(str)).dt.date
    return c


def chain_iv(chain, symbol, expiry, strike, right):
    d = chain[(chain.root == symbol) & (chain.exp == expiry) & (chain.right == right)]
    row = d[np.isclose(d.strike, strike)]
    if len(row) and row.iv.iloc[0] > 0:
        r = row.iloc[0]
        return float(r.iv), float(r.ask - r.bid)
    d = d[d.iv > 0].sort_values("strike")  # interpolate the smile when the strike is missing
    return float(np.interp(strike, d.strike, d.iv)), float(np.nan)


def contracts_for(count):
    from hedge.models import Contract

    out = [Contract(con_id=STOCK_ID[s], symbol=s, kind="STK") for s in ("SPCX", "RKLB", "SPY")]
    for con_id, symbol, strike, right, _ in MANDATED:
        out.append(Contract(con_id=con_id, symbol=symbol, kind="OPT", multiplier=100, expiry=N20,
                            strike=strike, right=right, trading_class=symbol, underlying_id=STOCK_ID[symbol]))
    ids = option_ids()
    for symbol, k in zip(("SPCX", "RKLB", "SPY"), SPLIT[count], strict=True):
        for expiry, strike, right in PRIORITY[symbol][:k]:
            out.append(Contract(con_id=ids[(symbol, expiry, float(strike), right)], symbol=symbol, kind="OPT",
                                multiplier=100, expiry=expiry, strike=float(strike), right=right,
                                trading_class=symbol, underlying_id=STOCK_ID[symbol]))
    return out


def daily_path():
    path = CASES / "daily-contest.csv"
    if not path.exists():
        CASES.mkdir(parents=True, exist_ok=True)
        d = pd.read_csv(DAILY)
        d[d.symbol.isin(["SPCX", "RKLB", "SPY"])][["date", "symbol", "close"]].to_csv(path, index=False)
    return path


def manifest_for(count, burn_surplus=False, policy=None):
    """small.contest.yaml with this case's contracts and the live template's policy switches."""
    from hedge.config import Manifest

    data = yaml.safe_load(BASE_YAML.read_text())
    contracts = contracts_for(count)
    data["contracts"] = [c.model_dump(mode="json") for c in contracts]
    data["mandatory"] = {str(i): q for i, _, _, _, q in MANDATED}
    data["universe"] = "allowlist"
    data["allowlist"] = [c.con_id for c in contracts if c.con_id not in dict((m[0], 1) for m in MANDATED)
                         and not (c.symbol == "SPY" and c.kind == "STK")]
    data["daily_stock_history"] = str(daily_path())
    data["confirmed"] = True
    data["policy"] = {**data["policy"], "burn_surplus": burn_surplus, "overnight_weight": 0.5, **(policy or {})}
    return Manifest.model_validate(data)


def clock_at(hhmm):
    from hedge.config import NY

    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(SESSION.year, SESSION.month, SESSION.day, h, m, tzinfo=NY)


def quote_table(hhmm):
    """{con_id: quote fields} for every contract of the largest case, cached per clock."""
    path = CASES / f"quotes_{hhmm.replace(':', '')}.json"
    if path.exists():
        return {int(k): v for k, v in json.loads(path.read_text()).items()}
    from hedge.valuation import ValuationEngine

    m = manifest_for(38)
    now = clock_at(hhmm)
    val = ValuationEngine(m)
    chain = chain_rows()
    table = {}
    for c in m.contracts:
        spot = SPOT[c.symbol]
        if c.kind == "STK":
            table[c.con_id] = dict(bid=round(spot - 0.01, 2), ask=round(spot + 0.01, 2), bid_size=500,
                                   ask_size=500, spot=spot, iv=None, delta=1.0, gamma=0.0, vega=0.0,
                                   theta=0.0, rho=None)
            continue
        iv, chain_spread = chain_iv(chain, c.symbol, c.expiry, c.strike, c.right)
        price = val.price(c, spot, iv, now)
        # Chain spread, kept inside the execution filter (<= 0.40 and <= 12% of the price).
        spread = chain_spread if np.isfinite(chain_spread) else 0.10
        spread = float(np.clip(spread, 0.02, min(0.40, 0.12 * price)))
        bid = max(0.01, round(price - spread / 2, 2))
        g = val.greeks(c, spot, iv, now)
        table[c.con_id] = dict(bid=bid, ask=round(bid + spread, 2), bid_size=20, ask_size=20, spot=spot,
                               iv=iv, model_price=price, **g)
    CASES.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, indent=1))
    return table


def build_case(count, hhmm, burn_surplus=False, gap=500.0, policy=None):
    """(manifest, quotes, snapshot, previous_nav, nav_estimate, now) for one case."""
    from hedge.models import PortfolioSnapshot, Position, Quote

    m = manifest_for(count, burn_surplus, policy)
    now = clock_at(hhmm)
    table = quote_table(hhmm)
    quotes = {}
    for c in m.contracts:
        f = dict(table[c.con_id])
        quotes[c.con_id] = Quote(con_id=c.con_id, observed_at=now, data_type="live", provider="moomoo",
                                 greek_at=now, shortable_shares=None, **f)
    nav = float(m.initial_nav) + gap
    mark = sum(quotes[i].mid * q * m.contract_map[i].multiplier for i, q in m.mandatory.items())
    snap = PortfolioSnapshot(
        account=m.account, at=now, account_at=now, nav=Decimal(str(round(nav, 2))),
        cash=Decimal(str(round(nav - mark, 2))), excess_liquidity=Decimal(str(round(0.8 * nav, 2))),
        initial_margin=Decimal(str(round(0.2 * nav, 2))),
        positions=tuple(Position(con_id=i, quantity=q) for i, q in m.mandatory.items()),
    )
    previous = float(m.initial_nav) * (1 + 0.0344 / 360)
    return m, quotes, snap, previous, nav, now

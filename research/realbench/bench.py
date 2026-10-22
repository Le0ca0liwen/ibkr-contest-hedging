"""Realistic contest solve-time benchmark: real spots/IVs, dry-run universe, mandated book held."""
import os, sys, time, json, warnings
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
CONFIG = os.environ.get("BENCH_CONFIG") or str(RESEARCH_ROOT / "config" / "local.contest.yaml")
repo = sys.argv[1]
sys.path.insert(0, repo)
warnings.filterwarnings("ignore")
from hedge.config import NY, Manifest
from hedge.models import PortfolioSnapshot, Position, Quote
from hedge.optimizer import HedgeOptimizer
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
from hedge.valuation import ValuationEngine

SPOT = {"SPCX": 147.24, "RKLB": 51.15, "SPY": 768.51, "IVV": 772.0, "VOO": 706.0}
IV = {"SPCX": 0.57, "RKLB": 0.79, "SPY": 0.135, "IVV": 0.135, "VOO": 0.135}
DAILY_VOL = {"SPCX": 0.040, "RKLB": 0.050, "SPY": 0.009, "IVV": 0.009, "VOO": 0.009}

def history(path, end):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=95)
    rng = np.random.default_rng(5)
    market = rng.normal(0, 0.009, len(days))
    rows = []
    for s in SPOT:
        beta = {"SPCX": 1.8, "RKLB": 2.2}.get(s, 1.0)
        idio = 0.0005 if s in ("SPY", "IVV", "VOO") else DAILY_VOL[s] * 0.7
        rets = beta * market + rng.normal(0, idio, len(days))
        px = SPOT[s] * np.exp(np.cumsum(rets) - rets.sum())
        rows += [{"date": str(d.date()), "symbol": s, "close": p} for d, p in zip(days, px)]
    pd.DataFrame(rows).to_csv(path, index=False)

def run(label, now, nav=988300.06, overrides=None):
    data = yaml.safe_load(Path(CONFIG).read_text())
    hist = RESEARCH_ROOT / "realbench" / f"daily-{now.date()}.csv"
    history(hist, now.date() - timedelta(days=1))
    data.update(daily_stock_history=str(hist), initial_nav=str(nav), **(overrides or {}))
    m = Manifest.model_validate(data)
    engine = ValuationEngine(m)
    quotes = {}
    for c in m.contracts:
        spot = SPOT[c.symbol]
        if c.kind == "STK":
            bid, ask, iv, g = spot - 0.01, spot + 0.01, None, {}
        else:
            price = engine.price(c, spot, IV[c.symbol], now)
            half = max(0.01, min(0.10, 0.02 * price))
            bid, ask, iv = max(0.01, price - half), price + half, IV[c.symbol]
            g = engine.greeks(c, spot, iv, now)
        quotes[c.con_id] = Quote(con_id=c.con_id, bid=bid, ask=ask, bid_size=50, ask_size=50, observed_at=now,
                                 data_type="live", provider="moomoo", spot=spot, iv=iv, greek_at=now if iv else None,
                                 model_price=None, shortable_shares=None, **g)
    pos = tuple(Position(con_id=i, quantity=q, average_cost=Decimal(1)) for i, q in m.mandatory.items())
    snap = PortfolioSnapshot(account=m.account, at=now, account_at=now, nav=Decimal(str(nav)), cash=Decimal("900000"),
                             excess_liquidity=Decimal("750000"), initial_margin=Decimal("150000"), positions=pos)
    ids = screen_options(m, quotes, snap.quantities())
    t0 = time.perf_counter(); sc = ScenarioEngine(m).build(snap, quotes, now, ids); t1 = time.perf_counter()
    opt = HedgeOptimizer(m)
    out = []
    for stock_only in (True, False):
        t2 = time.perf_counter()
        target = opt.solve(snap, quotes, sc, Decimal(str(nav)), stock_only=stock_only) if "stock_only" in opt.solve.__code__.co_varnames else opt.solve(snap, quotes, sc, Decimal(str(nav)))
        t3 = time.perf_counter()
        hedges = {m.contract_map[k].label: v for k, v in target.quantities.items() if v and k not in m.mandatory}
        out.append({"mode": "stock" if stock_only else "full", "status": target.status, "secs": round(t3 - t2, 2), "hedges": len(hedges)})
        if "stock_only" not in opt.solve.__code__.co_varnames:
            break
    print(json.dumps({"case": label, "screened": len(ids), "options": sum(m.contract_map[i].kind == "OPT" for i in ids),
                      "scenario_s": round(t1 - t0, 2), "solves": out}))

for label, when in [("Day0 13:00", datetime(2026, 10, 21, 13, 0, tzinfo=NY)),
                    ("Thu 15:40", datetime(2026, 10, 22, 15, 40, tzinfo=NY)),
                    ("Fri 15:40", datetime(2026, 10, 23, 15, 40, tzinfo=NY)),
                    ("Tue 10:30", datetime(2026, 10, 27, 10, 30, tzinfo=NY))]:
    policy = dict(yaml.safe_load(Path(CONFIG).read_text()).get("policy", {}))
    for item in sys.argv[2:]:
        key, value = item.split("=")
        policy[key] = float(value)
    run(label, when, overrides={"policy": policy})

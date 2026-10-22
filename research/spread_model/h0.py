"""Initial overnight hedge H0: one full app solve at Day 0 15:44 ET at today's spots (shared by all runs)."""
import json, sys
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
import time as _tm
from common import *
from hedge.scenarios import ScenarioEngine
from hedge.optimizer import HedgeOptimizer
from hedge.models import PortfolioSnapshot, Position, Quote
m = build_manifest()
eng = ValuationEngine(m)
inputs = RL.option_inputs()
at = datetime.combine(DAY_ZERO, dtime(15, 44), NY)
q = {}
for c in m.contracts:
    s = TODAY_SPOT[c.symbol]
    if c.kind == "STK":
        bid = round(s - STOCK_SPREAD[c.symbol] / 2, 2); ask = round(bid + STOCK_SPREAD[c.symbol], 2)
        q[c.con_id] = Quote(provider="synthetic", con_id=c.con_id, bid=bid, ask=ask, bid_size=500, ask_size=500, observed_at=at,
                            data_type="synthetic", spot=(bid + ask) / 2, model_price=s)
    else:
        meta = inputs[option_key(c)]; iv = float(meta["iv"])
        p = eng.price(c, s, iv, at); g = eng.greeks(c, s, iv, at)
        bid, ask = quote_from_price(p, float(meta["rel_spread"]))
        q[c.con_id] = Quote(provider="synthetic", con_id=c.con_id, bid=bid, ask=ask, bid_size=50, ask_size=50, observed_at=at,
                            data_type="synthetic", spot=s, iv=iv, greek_at=at, model_price=p, **g)
pos = dict(m.mandatory)
cash = Decimal(1000000) - sum(Decimal(str(q[i].mid)) * n * m.contract_map[i].multiplier for i, n in pos.items())
snap = PortfolioSnapshot(account=m.account, at=at, account_at=at, nav=Decimal(1000000), cash=cash, accrued_cash=Decimal(0),
    settled_cash=cash, excess_liquidity=Decimal(800000), initial_margin=Decimal(200000),
    positions=tuple(Position(con_id=i, quantity=n) for i, n in sorted(pos.items())), orders=(), connected=True, reconciled=True, source="synthetic")
sc = ScenarioEngine(m).build(snap, q, at, list(q))
t = _tm.perf_counter()
tg = HedgeOptimizer(m).solve(snap, q, sc, 1000000, 1000000.0, False, "dynamic_l1")
print("solve", round(_tm.perf_counter() - t, 2), tg.status, tg.current_score, tg.expected_score, tg.solver_metrics.get("accepted_stage"))
h0 = {str(i): v for i, v in tg.quantities.items()}
print({m.contract_map[int(i)].label: v for i, v in h0.items() if int(i) not in m.mandatory})
(LANE / "h0.json").write_text(json.dumps(h0))

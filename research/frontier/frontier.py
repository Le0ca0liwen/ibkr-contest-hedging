"""Frontier lane: hedge-universe variants priced at TODAY's market (2026-10-21 13:13 ET monitor),
solved through the REAL app path on scored day 2026-10-22:

    screen_snapshot -> ScenarioEngine.build -> HedgeOptimizer.solve(full, dynamic_l1)
    -> RiskGate.validate(..., 2 * target.estimated_cost)   (exactly as hedge/service.py optimize())

starting from the mandated book only.  One process per (variant, regime); JSON lines appended to
results.jsonl.  Read-only use of the repo at REPO (sys.path insert); never connects to a broker.

Quotes: spots = today's mids; option IV = app-model IV implied from today's monitor mid (or, for
strikes/expiries not on the monitor, Oct-20 chains.csv vendor IV x per-(symbol, expiry) scale that
makes chain IV match today's monitor IVs, C/P averaged; same-strike monitor IV used when the other
right is on the monitor).  At each Oct-22 clock the option mid is re-priced with the app model at
that clock (same spot/IV), then IV/Greeks are re-calibrated from the rounded mid exactly as
hedge/moomoo_data.py does.  Seeds 1..k perturb spots (SPCX/RKLB 1.5%, SPY 0.4% sd) and add a
parallel IV shift per symbol (1.5 vol pts sd).

Regimes:  live = every candidate's spread capped at $0.45 (inside the $0.50 / 15% execution gate, so
          all candidates stay live: the solve-time frontier);
          asq  = today's quoted spreads (monitor), Oct-20 chain spreads for strikes not on it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter, process_time

import numpy as np
import pandas as pd
import yaml

# research/ root (env RESEARCH_ROOT, default: the folder above this script) and the app source (env REPO,
# default: the repository root above research/; the study used commit 1973abb).
RESEARCH_ROOT = os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.environ.get("REPO") or os.path.dirname(RESEARCH_ROOT)
sys.path.insert(0, REPO)
warnings.filterwarnings("ignore")

from hedge.config import NY, Manifest  # noqa: E402
from hedge.models import Contract, PortfolioSnapshot, Position, Quote  # noqa: E402
from hedge.optimizer import HedgeOptimizer, execution_cost  # noqa: E402
from hedge.risk import RiskEngine, RiskGate  # noqa: E402
from hedge.scenarios import ScenarioEngine  # noqa: E402
from hedge.universe import screen_snapshot  # noqa: E402
from hedge.valuation import BelowModelFloor, ValuationEngine  # noqa: E402

LANE = os.path.join(RESEARCH_ROOT, "frontier")
CAL = json.load(open(f"{LANE}/calib.json"))
SPOT0 = CAL["spot"]
CHAINS = pd.read_csv(os.path.join(RESEARCH_ROOT, "marketdata", "econ", "chains.csv"))
NAV0 = 1_000_000.0
DAY = date(2026, 10, 22)

STK = {"SPCX": 10, "RKLB": 20, "SPY": 30}
STK_HALF = {"SPCX": 0.01, "RKLB": 0.005, "SPY": 0.01}  # today's stock spreads 0.02 / 0.01 / 0.02
MANDATED = [  # (sym, exp, K, right, qty)
    ("SPCX", "2026-11-20", 130, "C", 25), ("SPCX", "2026-11-20", 145, "C", -50), ("SPCX", "2026-11-20", 160, "C", 25),
    ("RKLB", "2026-11-20", 45, "P", -25), ("RKLB", "2026-11-20", 55, "P", 25),
    ("SPY", "2026-11-20", 770, "C", 25), ("SPY", "2026-11-20", 770, "P", 25),
]
V0 = [("RKLB", "2026-11-20", 50, "P"), ("RKLB", "2026-11-27", 50, "C"), ("RKLB", "2026-11-27", 50, "P"),
      ("SPCX", "2026-11-20", 145, "P"), ("SPCX", "2026-11-27", 145, "C"), ("SPCX", "2026-11-27", 145, "P"),
      ("SPY", "2026-11-13", 770, "C"), ("SPY", "2026-11-13", 770, "P"), ("SPY", "2026-11-27", 770, "C"),
      ("SPY", "2026-11-27", 770, "P")]
RECENTER = {("SPCX", "2026-11-20", 145, "P"): ("SPCX", "2026-11-20", 160, "P"),
            ("SPCX", "2026-11-27", 145, "C"): ("SPCX", "2026-11-27", 160, "C"),
            ("SPCX", "2026-11-27", 145, "P"): ("SPCX", "2026-11-27", 160, "P")}
V1 = [RECENTER.get(o, o) for o in V0]
SPCX27_160 = [("SPCX", "2026-11-27", 160, "C"), ("SPCX", "2026-11-27", 160, "P")]
RKLB27_55 = [("RKLB", "2026-11-27", 55, "C"), ("RKLB", "2026-11-27", 55, "P")]
RKLB27_53 = [("RKLB", "2026-11-27", 53, "C"), ("RKLB", "2026-11-27", 53, "P")]
VARIANTS = {
    "V0": V0,
    "V1": V1,
    "V2": V0 + SPCX27_160,
    "V3a": [o for o in V1 if not (o[0] == "RKLB" and o[1] == "2026-11-27")] + RKLB27_55,  # 12: RKLB Nov27 50 -> 55
    "V3b": V1 + RKLB27_55,  # 14
    "V3c": [o for o in V1 if not (o[0] == "RKLB" and o[1] == "2026-11-27")] + RKLB27_53,  # 12: RKLB Nov27 50 -> 53 (true ATM)
    "V4": V0 + SPCX27_160 + [("SPY", "2026-11-27", 775, "C"), ("SPY", "2026-11-27", 775, "P"), ("RKLB", "2026-11-27", 55, "P")],
    "V5": V1 + [("SPCX", "2026-11-13", 160, "C"), ("SPCX", "2026-11-13", 160, "P")],
}
ALL_OPTS = sorted({o for v in VARIANTS.values() for o in v})
CON = {o: 2001 + k for k, o in enumerate(ALL_OPTS)}
for k, (s, e, K, r, q) in enumerate(MANDATED):
    CON[(s, e, K, r)] = 1001 + k


def contract(spec):
    s, e, K, r = spec
    return Contract(con_id=CON[spec], symbol=s, kind="OPT", multiplier=100, expiry=date.fromisoformat(e),
                    strike=float(K), right=r, underlying_id=STK[s], trading_class=s)


def manifest(options, hist, seed=17, allow_all=False):
    base = yaml.safe_load(open(os.path.join(RESEARCH_ROOT, "config", "small.contest.yaml")))
    contracts = [Contract(con_id=STK[s], symbol=s, kind="STK") for s in STK]
    contracts += [contract(m[:4]) for m in MANDATED] + [contract(o) for o in options]
    policy = {**base["policy"], "primary_band": 20.0, "burn_surplus": False, "overnight_weight": 0.5, "seed": seed}
    base.update(
        contracts=[c.model_dump(mode="json") for c in contracts],
        mandatory={CON[m[:4]]: m[4] for m in MANDATED},
        universe="allowlist",
        allowlist=[STK["SPCX"], STK["RKLB"]] + [CON[o] for o in options],
        daily_stock_history=hist,
        policy=policy,
        confirmed=True,
        initial_nav=str(NAV0),
    )
    return Manifest.model_validate(base)


def base_iv(spec):
    s, e, K, r = spec
    key = f"{s}|{e}|{K}|{r}"
    if key in CAL["iv_today"]:
        return CAL["iv_today"][key], "monitor"
    other = f"{s}|{e}|{K}|{'P' if r == 'C' else 'C'}"
    if other in CAL["iv_today"]:
        return CAL["iv_today"][other], "monitor other right"
    x = CHAINS[(CHAINS.root == s) & (CHAINS.expiry == int(e.replace("-", ""))) & (CHAINS.strike == K)]
    scale = CAL["scale"].get(f"{s}|{e}") or CAL["scale"][f"{s}|2026-11-20"]
    return float(x.iv.mean()) * scale, f"chain x {scale:.4f}"


def base_spread(spec):
    s, e, K, r = spec
    key = f"{s}|{e}|{K}|{r}"
    if key in CAL["monitor"]:
        bid, ask = CAL["monitor"][key]
        return round(ask - bid, 2), "monitor"
    x = CHAINS[(CHAINS.root == s) & (CHAINS.expiry == int(e.replace("-", ""))) & (CHAINS.strike == K) & (CHAINS.right == r)].iloc[0]
    return round(float(x.ask - x.bid), 2), "chain Oct-20"


VAL = None


def make_quotes(m, now, qseed, regime):
    global VAL
    VAL = VAL or ValuationEngine(m)
    rng = np.random.default_rng(1000 + qseed)
    spot, ivshift = dict(SPOT0), {s: 0.0 for s in STK}
    if qseed:
        for s, sd in (("SPCX", 0.015), ("RKLB", 0.015), ("SPY", 0.004)):
            spot[s] = round(SPOT0[s] * (1 + rng.normal(0, sd)), 2)
            ivshift[s] = float(rng.normal(0, 0.015))
    quotes = {}
    for c in m.contracts:
        if c.kind == "STK":
            quotes[c.con_id] = Quote(provider="moomoo", con_id=c.con_id, bid=round(spot[c.symbol] - STK_HALF[c.symbol], 3),
                                     ask=round(spot[c.symbol] + STK_HALF[c.symbol], 3), bid_size=500, ask_size=500, observed_at=now,
                                     data_type="live", spot=spot[c.symbol], iv=None, greek_at=now,
                                     shortable_shares=m.stock_borrow.get(c.symbol))
            continue
        spec = (c.symbol, str(c.expiry), int(c.strike) if c.strike == int(c.strike) else c.strike, c.right)
        iv0 = max(0.02, base_iv(spec)[0] + ivshift[c.symbol])
        mid = VAL.price(c, spot[c.symbol], iv0, now)
        spread = base_spread(spec)[0]
        if regime == "live" and c.con_id not in m.mandatory:
            spread = min(spread, 0.45)
        bid = round(max(0.01, mid - spread / 2), 2)
        ask = round(bid + spread, 2)
        try:
            iv = VAL.implied_volatility(c, spot[c.symbol], (bid + ask) / 2, now)
        except BelowModelFloor:
            iv = iv0
        g = VAL.greeks(c, spot[c.symbol], iv, now)
        quotes[c.con_id] = Quote(provider="moomoo", con_id=c.con_id, bid=bid, ask=ask, bid_size=20, ask_size=20,
                                 observed_at=now, data_type="live", spot=spot[c.symbol], iv=iv, greek_at=now,
                                 model_price=VAL.price(c, spot[c.symbol], iv, now), **g)
    return quotes


def snapshot(m, quotes, now):
    mark = sum(quotes[i].mid * q * m.contract_map[i].multiplier for i, q in m.mandatory.items())
    return PortfolioSnapshot(account=m.account, at=now, account_at=now, nav=Decimal(str(NAV0)),
                             cash=Decimal(str(round(NAV0 - mark, 2))), excess_liquidity=Decimal("900000"),
                             initial_margin=Decimal("100000"),
                             positions=tuple(Position(con_id=i, quantity=q) for i, q in m.mandatory.items()))


def label(m, i):
    return m.contract_map[i].label


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variant", required=True)
    p.add_argument("--regime", default="live", choices=["live", "asq"])
    p.add_argument("--clocks", default="1000,1230,1400,1500,1540")
    p.add_argument("--qseeds", default="0,1,2")
    p.add_argument("--noscreen", action="store_true", help="bypass screen_snapshot (all allowlisted ids)")
    p.add_argument("--oos-seeds", default="101,102,103,104,105")
    p.add_argument("--hist", default=f"{LANE}/daily-contest.csv")
    p.add_argument("--out", default=f"{LANE}/results.jsonl")
    p.add_argument("--tag", default="")
    a = p.parse_args()
    opts = VARIANTS[a.variant]
    m = manifest(opts, a.hist)
    union = manifest(ALL_OPTS, a.hist)
    oos_manifests = [manifest(ALL_OPTS, a.hist, seed=int(s)) for s in a.oos_seeds.split(",") if s]
    engine = ScenarioEngine(m)
    opt, gate, risk = HedgeOptimizer(m), RiskGate(m), RiskEngine(m)
    previous = NAV0 * (1 + 0.0344 / 360) ** m.accrual_days(DAY)  # Oct 22 target: Day-0 close x (1+r/360)^1
    # Warm-up (imports/JIT/first canonicalization), not recorded.
    now = datetime(2026, 10, 22, 10, 0, tzinfo=NY)
    q = make_quotes(m, now, 0, a.regime)
    s = snapshot(m, q, now)
    sc = engine.build(s, q, now, screen_snapshot(m, q, s))
    opt.solve(s, q, sc, previous, float(s.nav), True, "dynamic_l1")
    first_build = True
    engine = ScenarioEngine(m)  # fresh caches: first recorded build is cold (covariance + betas)
    for clock in a.clocks.split(","):
        hh, mm = int(clock[:2]), int(clock[2:])
        now = datetime(2026, 10, 22, hh, mm, tzinfo=NY)
        for qseed in [int(x) for x in a.qseeds.split(",")]:
            uquotes = make_quotes(union, now, qseed, a.regime)  # union quotes: identical across variants
            quotes = {i: q for i, q in uquotes.items() if i in m.contract_map}
            snap = snapshot(m, quotes, now)
            t0 = perf_counter()
            ids = screen_snapshot(m, quotes, snap)
            screen_s = perf_counter() - t0
            if a.noscreen:
                ids = tuple(sorted(set(m.allowlist) | set(m.mandatory)))
            t0, c0 = perf_counter(), process_time()
            sc = engine.build(snap, quotes, now, ids)
            build_s, build_cpu = perf_counter() - t0, process_time() - c0
            t0, c0 = perf_counter(), process_time()
            sc_again = engine.build(snap, quotes, now, ids)  # pre-transmit rebuild (warm caches)
            rebuild_s, rebuild_cpu = perf_counter() - t0, process_time() - c0
            assert sc_again.identifier == sc.identifier
            nav_est = float(snap.nav)
            load_before = os.getloadavg()[0]
            t0, c0 = perf_counter(), process_time()
            target = opt.solve(snap, quotes, sc, previous, nav_est, False, "dynamic_l1")
            wall, solve_cpu = perf_counter() - t0, process_time() - c0
            sm = target.solver_metrics
            g = gate.validate(snap, quotes, sc, target.quantities, now, 2 * target.estimated_cost)
            unh = risk.evaluate(snap, quotes, sc, snap.quantities())
            corrective = not unh.passed
            threshold = max(m.policy.score_threshold, m.policy.uncertainty_multiplier * target.uncertainty)
            improvement = target.current_score - target.expected_score
            hedges = {label(m, i): v for i, v in target.quantities.items() if v and i not in m.mandatory}
            # Out-of-sample on the app model: same build path, other scenario seeds, union of all ids.
            oos = {"target": [], "book": []}
            uids = tuple(sorted(set(union.allowlist) | set(union.mandatory)))
            uopt = HedgeOptimizer(union)
            actual = snap.quantities()
            for um in oos_manifests:
                usc = ScenarioEngine(um).build(snap, uquotes, now, uids)
                tv = np.array([target.quantities.get(i, 0) for i in usc.ids])
                bv = np.array([actual.get(i, 0) for i in usc.ids])
                delta = tv - bv
                cost = target.estimated_cost
                cash_t = float(snap.cash) - sum(d * uquotes[i].mid * union.contract_map[i].multiplier
                                                for i, d in zip(usc.ids, delta)) - cost
                err = nav_est - previous
                oos["target"].append(uopt._score(snap, uquotes, usc, err, previous, tv, cash_t, cost)["score"])
                oos["book"].append(uopt._score(snap, uquotes, usc, err, previous, bv, float(snap.cash), 0)["score"])
            gm = g.metrics
            budget = gm.get("budget", NAV0)
            rec = {
                "tag": a.tag, "variant": a.variant, "regime": a.regime, "noscreen": a.noscreen,
                "clock": clock, "qseed": qseed,
                "spots": {k: quotes[v].spot for k, v in STK.items()},
                "allowlisted": len(m.allowlist), "screened_ids": len(ids),
                "screened_out": sorted(label(m, i) for i in m.allowlist if i not in ids),
                "candidates": sm.get("candidate_count"),
                "spread_gated": sorted(label(m, i) for i in ids if i in m.allowlist and i in quotes
                                       and m.contract_map[i].kind == "OPT"
                                       and __import__("hedge.order_policy", fromlist=["x"]).spread_error(m, m.contract_map[i], quotes[i])),
                "integer_variables": sm.get("integer_variables"), "scalar_variables": sm.get("scalar_variables"),
                "screen_s": round(screen_s, 4), "build_s": round(build_s, 3), "build_cold": first_build,
                "rebuild_s": round(rebuild_s, 3), "build_cpu_s": round(build_cpu, 3), "rebuild_cpu_s": round(rebuild_cpu, 3),
                "solve_cpu_s": round(solve_cpu, 3), "load_before_solve": load_before,
                "status": target.status, "wall_s": round(wall, 3), "solver_total_ms": sm.get("total_ms"),
                "stages": [{k: st.get(k) for k in ("stage", "status", "total_ms", "solver_ms", "nodes", "mip_gap")}
                           for st in sm.get("stages", [])],
                "accepted_stage": sm.get("accepted_stage"), "ranking_complete": sm.get("ranking_complete"),
                "current_score": round(target.current_score, 2), "expected_score": round(target.expected_score, 2),
                "components": {k: round(v, 2) for k, v in target.components.items()},
                "improvement": round(improvement, 2), "uncertainty": round(target.uncertainty, 2),
                "threshold": round(threshold, 2), "estimated_cost": round(target.estimated_cost, 2),
                "gate_passed": g.passed, "gate_reasons": list(g.reasons),
                "unhedged_risk_passed": unh.passed, "unhedged_reasons": list(unh.reasons),
                "trade_eligible": bool(g.passed and target.status == "OPTIMAL" and (improvement > threshold or corrective)),
                "stress_loss": round(gm.get("stress_loss", float("nan")), 0), "stress_limit": round(gm.get("stress_limit", float("nan")), 0),
                "stress_frac": round(gm.get("stress_loss", float("nan")) / budget, 5),
                "unhedged_stress_frac": round(unh.metrics.get("stress_loss", float("nan")) / budget, 5),
                "delta_frac": {s: round(gm.get(f"delta:{s}", 0) / budget, 4) for s in STK},
                "unhedged_delta_frac": {s: round(unh.metrics.get(f"delta:{s}", 0) / budget, 4) for s in STK},
                "beta_nav": round(gm.get("beta_nav:portfolio", float("nan")), 4),
                "gross_notional": round(gm.get("gross_notional", 0), 0),
                "hedges": hedges,
                "oos_target_score": round(float(np.mean(oos["target"])), 2) if oos["target"] else None,
                "oos_book_score": round(float(np.mean(oos["book"])), 2) if oos["book"] else None,
                "load1": os.getloadavg()[0],
            }
            first_build = False
            print(json.dumps(rec), flush=True)
            with open(a.out, "a") as f:
                f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()

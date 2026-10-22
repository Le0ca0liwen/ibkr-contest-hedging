"""Measure ScenarioEngine.build and HedgeOptimizer.solve vs universe size N (one N per process)."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import warnings
from time import perf_counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, build_manifest, make_quotes, make_snapshot, now_at, write_history  # noqa: E402

from hedge import valuation as valmod  # noqa: E402
from hedge.optimizer import HedgeOptimizer  # noqa: E402
from hedge.scenarios import ScenarioEngine  # noqa: E402
from hedge.universe import screen_options  # noqa: E402

CALLS = {"price": 0}
_orig_price = valmod.ValuationEngine.price


def counted_price(self, *a, **k):
    CALLS["price"] += 1
    return _orig_price(self, *a, **k)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--hour", type=int, default=10)
    p.add_argument("--minute", type=int, default=30)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--modes", default="stock,full")
    p.add_argument("--history-sessions", type=int, default=60)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    hist = OUT / f"history_{args.n}.csv"
    from common import MANDATED, extra_names

    names = (MANDATED + extra_names(max(0, args.n - 3)))[: args.n]
    write_history(names, hist)
    m, names = build_manifest(args.n, hist, policy={"history_sessions": args.history_sessions})
    now = now_at(args.hour, args.minute)
    t0 = perf_counter()
    quotes = make_quotes(m, names, now)
    quote_s = perf_counter() - t0
    snap = make_snapshot(m, quotes, now)
    ids = screen_options(m, quotes, snap.quantities())
    per_symbol = {}
    for i in ids:
        c = m.contract_map[i]
        per_symbol.setdefault(c.symbol, [0, 0])[0 if c.kind == "STK" else 1] += 1
    valmod.ValuationEngine.price = counted_price
    engine = ScenarioEngine(m)
    # Separate covariance timing (cached afterwards inside build)
    symbols = sorted({m.contract_map[i].symbol for i in ids})
    t0 = perf_counter()
    cov = engine.daily_covariance(now, symbols)
    cov_s = perf_counter() - t0
    engine.daily_cache.clear()
    CALLS["price"] = 0
    t0 = perf_counter()
    scenarios = engine.build(snap, quotes, now, ids)
    build_s = perf_counter() - t0
    build_prices = CALLS["price"]
    valmod.ValuationEngine.price = _orig_price
    eig = np.linalg.eigvalsh(cov)
    shocks_rank = int(np.linalg.matrix_rank(scenarios.changes[:, [k for k, i in enumerate(scenarios.ids) if m.contract_map[i].kind == "STK"]])) if any(m.contract_map[i].kind == "STK" for i in scenarios.ids) else 0
    report = {
        "tag": args.tag,
        "n_underlyings": args.n,
        "contracts_defined": len(m.contracts),
        "options_defined": sum(c.kind == "OPT" for c in m.contracts),
        "allowlist": len(m.allowlist),
        "screen_ids": len(ids),
        "screen_per_symbol_stk_opt_sample": {s: per_symbol[s] for s in list(sorted(per_symbol))[:4]},
        "quote_gen_s": round(quote_s, 2),
        "covariance_s": round(cov_s, 4),
        "cov_condition": float(eig.max() / eig.min()),
        "cov_min_eig": float(eig.min()),
        "scenario_build_s": round(build_s, 3),
        "scenario_price_calls": build_prices,
        "scenario_rows": int(len(scenarios.weights)),
        "stress_rows": int(len(scenarios.stress)),
        "stock_change_rank": shocks_rank,
        "source": scenarios.source,
        "now": now.isoformat(),
        "results": [],
    }
    print("BUILD", json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)
    previous = float(m.initial_nav) * (1 + 0.0344 / 360) ** 1
    nav = float(snap.nav)
    opt = HedgeOptimizer(m)
    for mode in args.modes.split(","):
        for _ in range(args.repeats):
            t0 = perf_counter()
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Solution may be inaccurate")
                target = opt.solve(snap, quotes, scenarios, previous, nav, mode == "stock")
            wall = perf_counter() - t0
            sm = target.solver_metrics
            held = {i: q for i, q in target.quantities.items() if i not in m.mandatory}
            report["results"].append(
                {
                    "mode": mode,
                    "wall_s": round(wall, 3),
                    "status": target.status,
                    "candidate_count": sm.get("candidate_count"),
                    "integer_variables": sm.get("integer_variables"),
                    "scalar_variables": sm.get("scalar_variables"),
                    "stress_rows_input": sm.get("stress_rows_input"),
                    "stress_rows_model": sm.get("stress_rows_model"),
                    "stages": [
                        {k: s.get(k) for k in ("stage", "status", "total_ms", "solver_ms", "nodes", "mip_gap")}
                        for s in sm.get("stages", [])
                    ],
                    "accepted_stage": sm.get("accepted_stage"),
                    "ranking_complete": sm.get("ranking_complete"),
                    "current_score": round(target.current_score, 2),
                    "expected_score": round(target.expected_score, 2),
                    "hedge_positions": len(held),
                    "hedge_symbols": sorted({m.contract_map[i].symbol for i in held}),
                }
            )
            print(json.dumps(report["results"][-1]), flush=True)
    report["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    with open(OUT / "results.jsonl", "a") as f:
        f.write(json.dumps(report) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=1))


if __name__ == "__main__":
    main()

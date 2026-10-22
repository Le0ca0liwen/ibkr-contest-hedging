"""Phase-logged scaling run: screen, covariance, ScenarioEngine.build, HedgeOptimizer.solve.

Writes one JSON line per phase to scale2.jsonl (flushed), so partial results survive
timeouts/OOM. Read-only use of repo code; synthetic quotes/history only.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import threading
import time
import warnings
from time import perf_counter

import cvxpy as cp
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MANDATED, OUT, build_manifest, extra_names, make_quotes, make_snapshot, now_at, write_history  # noqa: E402

from hedge.optimizer import HedgeOptimizer  # noqa: E402
from hedge.scenarios import ScenarioEngine  # noqa: E402
from hedge.universe import screen_options  # noqa: E402

LOG = OUT / "scale3.jsonl"
PEAK = {"rss_mb": 0.0}
CANON = []


def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def sampler():
    while True:
        PEAK["rss_mb"] = max(PEAK["rss_mb"], rss_mb())
        time.sleep(0.2)


_gpd = cp.Problem.get_problem_data


def gpd(self, *a, **k):
    t = perf_counter()
    r = _gpd(self, *a, **k)
    CANON.append(round(perf_counter() - t, 3))
    return r


cp.Problem.get_problem_data = gpd


def emit(rec):
    rec = {**rec, "rss_now_mb": round(rss_mb(), 1), "rss_peak_mb": round(PEAK["rss_mb"], 1)}
    print(json.dumps(rec), flush=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--opts", type=int, default=12, help="options DEFINED per extra name (12 = all pass)")
    p.add_argument("--modes", default="stock,full")
    p.add_argument("--hour", type=int, default=10)
    p.add_argument("--minute", type=int, default=30)
    p.add_argument("--history-sessions", type=int, default=60)
    p.add_argument("--mem-gb", type=float, default=13)
    p.add_argument("--skip-build-solve", action="store_true")
    p.add_argument("--band", type=float, default=20)
    p.add_argument("--keep", type=int, default=None)
    p.add_argument("--load", action="store_true")
    p.add_argument("--extra-keep", type=int, default=None, help="options kept per NON-mandated symbol")
    p.add_argument("--tag", default="")
    args = p.parse_args()
    lim = int(args.mem_gb * 1024**3)
    resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    threading.Thread(target=sampler, daemon=True).start()
    base = {"tag": args.tag, "band": args.band, "keep": args.keep, "extra_keep": args.extra_keep, "n": args.n, "opts_defined": args.opts, "clock": f"{args.hour}:{args.minute:02d}"}
    names = (MANDATED + extra_names(max(0, args.n - 3)))[: args.n]
    hist = OUT / f"history_{args.n}.csv"
    write_history(names, hist)
    m, names = build_manifest(args.n, hist, options_per_name=args.opts,
                              policy={"history_sessions": args.history_sessions, "primary_band": args.band})
    now = now_at(args.hour, args.minute)
    quotes = make_quotes(m, names, now)
    snap = make_snapshot(m, quotes, now)
    t = perf_counter()
    ids = screen_options(m, quotes, snap.quantities())
    screen_s = perf_counter() - t
    if args.keep is not None:  # experiment: keep only the `keep` nearest-money screened options per symbol
        kept, by_sym = [], {}
        for i in ids:
            c = m.contract_map[i]
            if c.kind == "STK" or i in m.mandatory:
                kept.append(i)
            else:
                by_sym.setdefault(c.symbol, []).append(c)
        mandated_syms = {m.contract_map[i].symbol for i in m.mandatory}
        for sym, cs in by_sym.items():
            cs.sort(key=lambda c: (abs(c.strike / quotes[c.con_id].spot - 1), c.con_id))
            limit = args.keep if (sym in mandated_syms or args.extra_keep is None) else args.extra_keep
            kept += [c.con_id for c in cs[: limit]]
        ids = tuple(sorted(kept))
    opts_by_sym = {}
    for i in ids:
        c = m.contract_map[i]
        if c.kind == "OPT" and i not in m.mandatory:
            opts_by_sym[c.symbol] = opts_by_sym.get(c.symbol, 0) + 1
    engine = ScenarioEngine(m)
    symbols = sorted({m.contract_map[i].symbol for i in ids})
    t = perf_counter()
    cov = engine.daily_covariance(now, symbols)
    cov_s = perf_counter() - t
    eig = np.linalg.eigvalsh(cov)
    emit({**base, "phase": "screen", "contracts_defined": len(m.contracts),
          "options_defined": sum(c.kind == "OPT" for c in m.contracts), "allowlist": len(m.allowlist),
          "screen_ids": len(ids), "screen_s": round(screen_s, 3),
          "screen_options_per_symbol_minmax": [min(opts_by_sym.values(), default=0), max(opts_by_sym.values(), default=0)], "ids_after_keep": len(ids),
          "symbols": len(symbols), "covariance_s": round(cov_s, 4),
          "cov_condition": round(float(eig[-1] / eig[0]), 1)})
    if args.skip_build_solve:
        return
    import pickle
    state = OUT / f"state_n{args.n}_o{args.opts}_k{args.keep}_x{args.extra_keep}_{args.hour}{args.minute:02d}.pkl"
    if args.load:
        with open(state, "rb") as f:
            sc = pickle.load(f)
        build_s = float("nan")
    else:
        engine.daily_cache.clear()
        t = perf_counter()
        sc = engine.build(snap, quotes, now, ids)
        build_s = perf_counter() - t
        with open(state, "wb") as f:
            pickle.dump(sc, f)
    stk = [k for k, i in enumerate(sc.ids) if m.contract_map[i].kind == "STK"]
    emit({**base, "phase": "build", "scenario_build_s": round(build_s, 2), "scenario_rows": len(sc.weights),
          "stress_rows": len(sc.stress), "ids": len(sc.ids),
          "changes_rank": int(np.linalg.matrix_rank(sc.changes)),
          "stock_changes_rank": int(np.linalg.matrix_rank(sc.changes[:, stk])) if stk else 0,
          "source": sc.source})
    previous = float(m.initial_nav) * (1 + 0.0344 / 360)
    opt = HedgeOptimizer(m)
    for mode in args.modes.split(","):
        CANON.clear()
        t = perf_counter()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                target = opt.solve(snap, quotes, sc, previous, float(snap.nav), mode == "stock")
        except MemoryError:
            emit({**base, "phase": f"solve_{mode}", "status": "MemoryError", "wall_s": round(perf_counter() - t, 1),
                  "canon_s": CANON, "mem_cap_gb": args.mem_gb})
            continue
        wall = perf_counter() - t
        sm = target.solver_metrics
        emit({**base, "phase": f"solve_{mode}", "wall_s": round(wall, 2), "status": target.status,
              "candidates": sm.get("candidate_count"), "integer_variables": sm.get("integer_variables"),
              "scalar_variables": sm.get("scalar_variables"), "stress_rows_model": sm.get("stress_rows_model"),
              "canon_s": CANON,
              "stages": [{k: s.get(k) for k in ("stage", "status", "total_ms", "solver_ms", "nodes", "mip_gap")}
                         for s in sm.get("stages", [])],
              "current_score": round(target.current_score, 1), "expected_score": round(target.expected_score, 1),
              "hedge_positions": sum(1 for i in target.quantities if i not in m.mandatory)})


if __name__ == "__main__":
    main()

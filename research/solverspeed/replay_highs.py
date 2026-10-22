"""Replay a dumped stage model in HiGHS alone (no CVXPY), with the app's options.

    python replay_highs.py <stage.mps> [--stage primary|turnover|next_close]
        [--time-limit 10] [--abs-gap X] [--max-nodes N] [--warm <stage.start.npy>] [--analysis]
        [--opt name=value ...] [--reps 3]

Defaults mirror HedgeOptimizer.stage(): threads=1, random_seed=17, mip_rel_gap=0,
mip_abs_gap=primary_band/4=5 (primary) or 1e-6 (turnover, mip_max_nodes=ranking_nodes=32,
time_limit=ranking_seconds=1). Dump models with harness.py --dump-mps DIR.
--analysis turns on highs_analysis_level=128 (HiGHS MIP clock report) and prints the log.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

import highspy
import numpy as np


def run_once(path, opts, warm, log):
    h = highspy.Highs()
    h.setOptionValue("output_flag", bool(log))
    h.setOptionValue("log_to_console", False)
    if log:
        h.setOptionValue("log_file", str(log))
    h.readModel(str(path))
    for k, v in opts.items():
        if h.setOptionValue(k, v) != highspy.HighsStatus.kOk:
            raise SystemExit(f"bad option {k}={v}")
    if warm is not None:
        sol = highspy.HighsSolution()
        sol.col_value = list(warm)
        sol.value_valid = True
        h.setSolution(sol)
    t0 = perf_counter()
    h.run()
    wall = perf_counter() - t0
    info = h.getInfo()
    return {
        "wall": wall,
        "status": h.getModelStatus().name,
        "obj": info.objective_function_value,
        "bound": info.mip_dual_bound,
        "gap": info.mip_gap,
        "nodes": info.mip_node_count,
        "lp_iters": info.simplex_iteration_count,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mps")
    p.add_argument("--stage", default="primary")
    p.add_argument("--time-limit", type=float)
    p.add_argument("--abs-gap", type=float)
    p.add_argument("--max-nodes", type=int)
    p.add_argument("--warm")
    p.add_argument("--analysis", action="store_true")
    p.add_argument("--opt", action="append", default=[])
    p.add_argument("--reps", type=int, default=1)
    args = p.parse_args()
    opts = {"threads": 1, "random_seed": 17, "mip_rel_gap": 0.0}
    if args.stage == "primary":
        opts.update(mip_abs_gap=5.0, time_limit=10.0)
    elif args.stage == "next_close":
        opts.update(mip_abs_gap=0.25, time_limit=0.5, mip_max_nodes=32)
    else:
        opts.update(mip_abs_gap=1e-6, time_limit=1.0, mip_max_nodes=32)
    if args.time_limit is not None:
        opts["time_limit"] = args.time_limit
    if args.abs_gap is not None:
        opts["mip_abs_gap"] = args.abs_gap
    if args.max_nodes is not None:
        opts["mip_max_nodes"] = args.max_nodes
    for item in args.opt:
        k, v = item.split("=", 1)
        for cast in (int, float):
            try:
                v = cast(v)
                break
            except ValueError:
                pass
        if v in ("true", "false"):
            v = v == "true"
        opts[k] = v
    if args.analysis:
        opts["highs_analysis_level"] = 128
    warm = np.load(args.warm) if args.warm else None
    results = []
    for k in range(args.reps):
        log = Path(tempfile.mkstemp(suffix=".log")[1]) if (args.analysis and k == 0) else None
        r = run_once(args.mps, opts, warm, log)
        results.append(r)
        print({k2: (round(v, 4) if isinstance(v, float) else v) for k2, v in r.items()}, flush=True)
        if log is not None:
            print(log.read_text())
    if len(results) > 1:
        w = [r["wall"] for r in results]
        print(f"median {statistics.median(w):.3f} min {min(w):.3f} max {max(w):.3f}")


if __name__ == "__main__":
    sys.exit(main())

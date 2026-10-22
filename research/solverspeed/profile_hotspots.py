"""Hotspot profiles for one case: cProfile (+ optional pyinstrument) of build and solve.

    HEDGE_SRC=<src> python profile_hotspots.py --n 38 --clock 15:40 [--what solve,build,canon]
        [--pyinstrument] [--top 40]

Writes <lane>/prof/<tag>_n<N>_<HHMM>_<what>.prof (load with pstats / snakeviz) and prints:
  - top functions by cumulative and by self time,
  - for 'canon': the first get_problem_data split by CVXPY reduction (apply of each reduction)
    and by expression-tree work (canonicalize_tree / canon backend / tensor building).
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import warnings
from pathlib import Path
from time import perf_counter

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
SRC = os.environ.get("HEDGE_SRC") or os.environ.get("REPO") or str(RESEARCH_ROOT.parent)
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parent / "pylib"))  # optional vendored pyinstrument; else pip install pyinstrument

import cvxpy as cp  # noqa: E402

import cases  # noqa: E402

LANE = RESEARCH_ROOT / "solverspeed"


def show(stats, sort, top, restrict=None):
    out = io.StringIO()
    stats.stream = out
    stats.sort_stats(sort)
    if restrict:
        stats.print_stats(restrict, top)
    else:
        stats.print_stats(top)
    text = out.getvalue()
    start = text.find("   ncalls")
    return text[start:] if start >= 0 else text


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=38)
    p.add_argument("--clock", default="15:40")
    p.add_argument("--what", default="canon,solve,build")
    p.add_argument("--top", type=int, default=35)
    p.add_argument("--pyinstrument", action="store_true")
    p.add_argument("--tag", default="base")
    args = p.parse_args()
    warnings.simplefilter("ignore")
    from hedge.optimizer import HedgeOptimizer
    from hedge.scenarios import ScenarioEngine
    from hedge.universe import screen_options

    m, quotes, snap, prev, nav, now = cases.build_case(args.n, args.clock)
    ids = screen_options(m, quotes, snap.quantities())
    engine = ScenarioEngine(m)
    scen = engine.build(snap, quotes, now, ids)  # warm caches, QuantLib, imports
    opt = HedgeOptimizer(m)
    (LANE / "prof").mkdir(exist_ok=True)
    stem = f"{args.tag}_n{args.n}_{args.clock.replace(':', '')}"
    what = args.what.split(",")

    if "canon" in what:
        # Capture the problem from a real solve, then profile a fresh first canonicalization of an
        # identical problem built by a second solve (canonicalization is per Problem object).
        captured = {}
        orig_gpd = cp.Problem.get_problem_data

        def gpd(self, *a, **k):
            if "problem" not in captured:
                captured["problem"] = self
                prof = cProfile.Profile()
                t0 = perf_counter()
                prof.enable()
                try:
                    return orig_gpd(self, *a, **k)
                finally:
                    prof.disable()
                    captured["wall"] = perf_counter() - t0
                    captured["prof"] = prof
            return orig_gpd(self, *a, **k)

        cp.Problem.get_problem_data = gpd
        opt.solve(snap, quotes, scen, prev, nav)
        cp.Problem.get_problem_data = orig_gpd
        prof = captured["prof"]
        prof.dump_stats(LANE / "prof" / f"{stem}_canon.prof")
        st = pstats.Stats(prof)
        print(f"=== first get_problem_data (canonicalization) n={args.n} {args.clock}: wall {captured['wall']:.3f}s "
              f"(cProfile-inflated) ===")
        print(show(st, "cumulative", args.top))
        print("--- by self time ---")
        print(show(st, "tottime", args.top))
        # Per-reduction split
        print("--- reductions (cumulative) ---")
        print(show(st, "cumulative", 40, r"(apply|canonicalize_tree|canonicalize_expr|build_matrix|get_problem_matrix|"
                                           r"reduce|construct_solving_chain|_construct_chain|is_dpp|is_dcp|"
                                           r"get_coeffs|tensor|backend|lin_op|ConeMatrixStuffing|Dcp2Cone)"))

    if "solve" in what:
        prof = cProfile.Profile()
        t0 = perf_counter()
        prof.enable()
        target = opt.solve(snap, quotes, scen, prev, nav)
        prof.disable()
        wall = perf_counter() - t0
        prof.dump_stats(LANE / "prof" / f"{stem}_solve.prof")
        st = pstats.Stats(prof)
        print(f"=== HedgeOptimizer.solve n={args.n} {args.clock}: {target.status} wall {wall:.3f}s (cProfile) ===")
        print(show(st, "cumulative", args.top))
        print("--- by self time ---")
        print(show(st, "tottime", args.top))
        if args.pyinstrument:
            from pyinstrument import Profiler

            pr = Profiler(interval=0.0005)
            pr.start()
            target = opt.solve(snap, quotes, scen, prev, nav)
            pr.stop()
            print(f"=== pyinstrument solve: {target.status} ===")
            print(pr.output_text(unicode=False, color=False, show_all=False))

    if "build" in what:
        prof = cProfile.Profile()
        t0 = perf_counter()
        prof.enable()
        engine.build(snap, quotes, now, ids)
        prof.disable()
        wall = perf_counter() - t0
        prof.dump_stats(LANE / "prof" / f"{stem}_build.prof")
        st = pstats.Stats(prof)
        print(f"=== ScenarioEngine.build (warm) n={args.n} {args.clock}: wall {wall:.3f}s (cProfile) ===")
        print(show(st, "cumulative", args.top))
        print("--- by self time ---")
        print(show(st, "tottime", args.top))
        if args.pyinstrument:
            from pyinstrument import Profiler

            pr = Profiler(interval=0.0002)
            pr.start()
            engine.build(snap, quotes, now, ids)
            pr.stop()
            print("=== pyinstrument build (warm) ===")
            print(pr.output_text(unicode=False, color=False, show_all=False))


if __name__ == "__main__":
    main()

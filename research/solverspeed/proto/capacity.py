"""Solve wall/status vs candidate count (intermediate sizes included) for baseline or prototype A.

    python capacity.py base|A  [--cands 20,23,26,29,32] [--reps 1] [--out file.jsonl]
"""
import argparse, json, sys, time, warnings
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[3])]
warnings.simplefilter("ignore")
import cases, load
cases.SPLIT.update({23: (7, 8, 8), 29: (9, 9, 9), 32: (10, 10, 10), 35: (11, 11, 11)})
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
ap = argparse.ArgumentParser(); ap.add_argument("which"); ap.add_argument("--cands", default="20,23,26,29,32")
ap.add_argument("--clocks", default="10:00,14:00,15:40"); ap.add_argument("--reps", type=int, default=1); ap.add_argument("--out", default=None)
a = ap.parse_args()
Opt = __import__("hedge.optimizer", fromlist=["x"]).HedgeOptimizer if a.which == "base" else load.proto_optimizer().HedgeOptimizer
out = open(a.out, "a") if a.out else None
for n in map(int, a.cands.split(",")):
    for clock in a.clocks.split(","):
        m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
        ids = screen_options(m, quotes, snap.quantities()); eng = ScenarioEngine(m)
        t = time.perf_counter(); scen = eng.build(snap, quotes, now, ids); build = time.perf_counter() - t
        for rep in range(a.reps):
            c0 = time.process_time(); t = time.perf_counter(); r = Opt(m).solve(snap, quotes, scen, prev, nav); wall = time.perf_counter() - t; cpu = time.process_time() - c0
            sm = r.solver_metrics
            rec = dict(which=a.which, n=n, clock=clock, rep=rep, cand=sm.get("candidate_count"), status=r.status, wall=round(wall, 3), cpu=round(cpu, 3), build=round(build, 3),
                       stages=[(s["stage"], s["status"], round(s["solver_ms"]), s.get("nodes")) for s in sm["stages"]], ranking=sm.get("ranking_complete"))
            print(json.dumps(rec), flush=True)
            if out: out.write(json.dumps(rec) + "\n"); out.flush()

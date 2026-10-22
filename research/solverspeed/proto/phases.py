"""Phase split for the prototype: construct+compile vs HiGHS time."""
import sys, time, warnings
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[3])]
warnings.simplefilter("ignore")
import cases, load
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
P = load.proto_optimizer()
for n in (12, 20, 38):
    for clock in ("10:00", "15:40"):
        m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
        scen = ScenarioEngine(m).build(snap, quotes, now, screen_options(m, quotes, snap.quantities()))
        r = P.HedgeOptimizer(m).solve(snap, quotes, scen, prev, nav)
        sm = r.solver_metrics; hs = sum(s["solver_ms"] for s in sm["stages"])
        print(n, clock, r.status, "total_ms", sm["total_ms"], "highs_ms", round(hs), "non-HiGHS ms", round(sm["total_ms"] - hs))

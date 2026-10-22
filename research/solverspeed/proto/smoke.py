import sys, time, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.simplefilter("ignore")
import cases, load
from hedge.optimizer import HedgeOptimizer
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
P = load.proto_optimizer()
n, clock = int(sys.argv[1]), sys.argv[2]
m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
ids = screen_options(m, quotes, snap.quantities())
scen = ScenarioEngine(m).build(snap, quotes, now, ids)
for name, cls in (("base", HedgeOptimizer), ("proto", P.HedgeOptimizer)):
    t = time.perf_counter(); r = cls(m).solve(snap, quotes, scen, prev, nav); dt = time.perf_counter() - t
    sm = r.solver_metrics
    print(name, round(dt, 3), r.status, round(r.expected_score, 4), sm.get("primary_lower_bound"), sm.get("integer_variables"), sm.get("scalar_variables"),
          [(s["stage"], s["status"], s.get("nodes"), s.get("incumbent")) for s in sm["stages"]], dict(sorted(r.quantities.items())))

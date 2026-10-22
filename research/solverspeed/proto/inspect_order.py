import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3])); sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore")
import cvxpy as cp, numpy as np, cases
import cvxpy.settings as s
from hedge.optimizer import HedgeOptimizer
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
CAP = {}
orig = cp.Problem.get_problem_data
def gpd(self, *a, **k):
    r = orig(self, *a, **k); CAP.setdefault("d", (self, r)); return r
cp.Problem.get_problem_data = gpd
m, quotes, snap, prev, nav, now = cases.build_case(12, "10:00")
ids = screen_options(m, quotes, snap.quantities())
scen = ScenarioEngine(m).build(snap, quotes, now, ids)
m.policy.__dict__["solver_seconds"] = 10
HedgeOptimizer(m).solve(snap, quotes, scen, prev, nav)
prob, (data, chain, inv) = CAP["d"]
pp = data[s.PARAM_PROB]
print("x vars:", [(v.id, v.size, v.attributes.get("integer"), v.attributes.get("boolean"), v.attributes.get("nonneg")) for v in pp.variables][:80])
print("orig var ids in problem.variables():", [(v.id, v.size) for v in prob.variables()])
print("dims", data[s.DIMS], "A", data[s.A].shape, "nnz", data[s.A].nnz)
print("constraints in param prob:", [(type(c).__name__, c.size) for c in pp.constraints][:200])

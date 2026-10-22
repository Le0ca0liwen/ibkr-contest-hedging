"""Save the primary-stage LP (and the options used) of each hard case for option sweeps."""
import sys, warnings
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[3])]
warnings.simplefilter("ignore")
import numpy as np, highspy, cases, load
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
P = load.proto_optimizer()
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
orig = P._HighsProblem.solve
state = {}
def solve(self, weight, options):
    if state.get("first"):
        state["first"] = False
        d = self.data
        np.savez(state["path"], A_start=d["A"].indptr, A_index=d["A"].indices, A_value=d["A"].data, shape=np.array(d["A"].shape),
                 cost=sum(w * r for w, r in zip(weight, d["objective"])), row_lower=d["row_lower"], row_upper=self.row_upper,
                 col_lower=d["col_lower"], col_upper=d["col_upper"], integer=d["integer"], abs_gap=options["mip_abs_gap"])
        raise KeyboardInterrupt
    return orig(self, weight, options)
P._HighsProblem.solve = solve
for n, clock in [(20, "15:40"), (26, "10:00"), (26, "14:00"), (26, "15:40"), (38, "10:00"), (38, "14:00"), (38, "15:40"), (17, "14:00")]:
    m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
    scen = ScenarioEngine(m).build(snap, quotes, now, screen_options(m, quotes, snap.quantities()))
    state.update(first=True, path=out / f"n{n}_{clock.replace(':','')}.npz")
    try:
        P.HedgeOptimizer(m).solve(snap, quotes, scen, prev, nav)
    except KeyboardInterrupt:
        print("saved", state["path"])

"""HiGHS option sweep on a saved primary-stage LP: wall time to a certified result (abs gap = band/4).

    python sweep.py LP.npz 'name={json options}' ...   (one config per arg; each run is one process-level solve)
"""
import json, sys, time
import numpy as np, highspy
from scipy import sparse

def load(path):
    z = np.load(path)
    return {k: z[k] for k in z.files}

def run(d, opts, limit=20.0):
    m = highspy.HighsModel(); lp = m.lp_
    r, c = d["shape"]; lp.num_col_, lp.num_row_ = int(c), int(r)
    lp.col_cost_ = d["cost"]; lp.row_lower_ = d["row_lower"]; lp.row_upper_ = d["row_upper"]
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = d["A_start"]; lp.a_matrix_.index_ = d["A_index"]; lp.a_matrix_.value_ = d["A_value"]
    lp.integrality_ = [highspy.HighsVarType.kInteger if f else highspy.HighsVarType.kContinuous for f in d["integer"]]
    lp.col_lower_ = d["col_lower"]; lp.col_upper_ = d["col_upper"]
    h = highspy.Highs(); h.setOptionValue("log_to_console", False)
    base = {"threads": 1, "random_seed": 17, "time_limit": limit, "mip_rel_gap": 0.0, "mip_abs_gap": float(d["abs_gap"])}
    for k, v in {**base, **opts}.items():
        if h.setOptionValue(k, v) == highspy.HighsStatus.kError:
            raise ValueError(f"bad option {k}={v}")
    h.passModel(m); t = time.perf_counter(); h.run(); wall = time.perf_counter() - t
    info = h.getInfo(); st = h.getModelStatus().name
    ok = st == "kOptimal" and np.isfinite(info.mip_dual_bound) and info.objective_function_value - info.mip_dual_bound <= float(d["abs_gap"]) + 1e-5
    return dict(wall=round(wall, 3), status=st, ok=bool(ok), obj=round(info.objective_function_value, 4), bound=round(info.mip_dual_bound, 4), nodes=int(info.mip_node_count))

if __name__ == "__main__":
    d = load(sys.argv[1])
    for spec in sys.argv[2:]:
        name, _, js = spec.partition("=")
        print(Path(sys.argv[1]).stem if False else sys.argv[1].split("/")[-1][:-4], name, json.dumps(run(d, json.loads(js or "{}"))), flush=True)

"""Diff the HiGHS models the baseline (CVXPY) and prototype A pass to HiGHS, stage by stage."""
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3])); sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.simplefilter("ignore")
import highspy, numpy as np, cases, load

CAPTURED = []
_orig = highspy.Highs.passModel
def _pass(self, model, *a, **k):
    lp = model.lp_
    CAPTURED.append({k_: np.array(getattr(lp, k_)) for k_ in ("col_cost_", "col_lower_", "col_upper_", "row_lower_", "row_upper_")}
                    | {"start": np.array(lp.a_matrix_.start_), "index": np.array(lp.a_matrix_.index_), "value": np.array(lp.a_matrix_.value_),
                       "integrality": np.array([int(v) for v in lp.integrality_]), "shape": (lp.num_row_, lp.num_col_)})
    return _orig(self, model, *a, **k)
highspy.Highs.passModel = _pass


def dense(d):
    from scipy import sparse
    r, c = d["shape"]
    return sparse.csc_matrix((d["value"], d["index"], d["start"]), shape=(r, c)).toarray()


def compare(a, b):
    out = {}
    if a["shape"] != b["shape"]:
        return {"shape": (a["shape"], b["shape"])}
    A, B = dense(a), dense(b)
    out["A_exact"] = bool(np.array_equal(A, B))
    out["A_maxdiff"] = float(np.max(np.abs(A - B))) if A.size else 0.0
    out["A_rows_sign_flipped"] = int(sum(1 for i in range(len(A)) if not np.array_equal(A[i], B[i]) and np.array_equal(A[i], -B[i])))
    for k in ("col_cost_", "col_lower_", "col_upper_", "row_lower_", "row_upper_", "integrality"):
        x, y = a[k], b[k]
        same = x.shape == y.shape and bool(np.array_equal(x, y))
        out[k] = same if same else (float(np.nanmax(np.abs(np.where(np.isinf(x) & np.isinf(y) & (x == y), 0, x - y)))) if x.shape == y.shape else "shape")
    return out


def run_pair(m, quotes, snap, scen, prev, nav, stock_only=False):
    from hedge.optimizer import HedgeOptimizer
    P = load.proto_optimizer()
    CAPTURED.clear(); rb = HedgeOptimizer(m).solve(snap, quotes, scen, prev, nav, stock_only); base = list(CAPTURED)
    CAPTURED.clear(); rp = P.HedgeOptimizer(m).solve(snap, quotes, scen, prev, nav, stock_only); proto = list(CAPTURED)
    return rb, rp, base, proto


if __name__ == "__main__":
    from hedge.scenarios import ScenarioEngine
    from hedge.universe import screen_options
    n, clock = int(sys.argv[1]), sys.argv[2]
    m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
    ids = screen_options(m, quotes, snap.quantities())
    scen = ScenarioEngine(m).build(snap, quotes, now, ids)
    rb, rp, base, proto = run_pair(m, quotes, snap, scen, prev, nav)
    for k, (a, b) in enumerate(zip(base, proto)):
        print("stage", k, compare(a, b))
    print("stages", len(base), len(proto), rb.status, rp.status, rb.quantities == rp.quantities)

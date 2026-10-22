"""Does a previous solve's solution (other clock, same columns) speed up the primary stage? Integer part only is used."""
import sys, time
import numpy as np, highspy
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import sweep

def model(d):
    m = highspy.HighsModel(); lp = m.lp_
    r, c = d["shape"]; lp.num_col_, lp.num_row_ = int(c), int(r)
    lp.col_cost_ = d["cost"]; lp.row_lower_ = d["row_lower"]; lp.row_upper_ = d["row_upper"]
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = d["A_start"]; lp.a_matrix_.index_ = d["A_index"]; lp.a_matrix_.value_ = d["A_value"]
    lp.integrality_ = [highspy.HighsVarType.kInteger if f else highspy.HighsVarType.kContinuous for f in d["integer"]]
    lp.col_lower_ = d["col_lower"]; lp.col_upper_ = d["col_upper"]
    return m

def solve(d, start=None, seed=17):
    h = highspy.Highs(); h.setOptionValue("log_to_console", False)
    for k, v in {"threads": 1, "random_seed": seed, "time_limit": 20.0, "mip_rel_gap": 0.0, "mip_abs_gap": float(d["abs_gap"])}.items():
        h.setOptionValue(k, v)
    h.passModel(model(d))
    if start is not None:
        s = highspy.HighsSolution(); s.col_value = list(start); h.setSolution(s)
    t = time.perf_counter(); h.run(); w = time.perf_counter() - t
    return w, h.getSolution().col_value, h.getInfo().objective_function_value, h.getInfo().mip_node_count, h.getModelStatus().name

root = "solverspeed/out/lps/"
for a, b in [("n26_1000", "n26_1400"), ("n26_1400", "n26_1540"), ("n38_1000", "n38_1540"), ("n38_1540", "n38_1000"), ("n38_1540", "n38_1400")]:
    da, db = sweep.load(root + a + ".npz"), sweep.load(root + b + ".npz")
    wa, xa, *_ = solve(da)
    cold = solve(db)
    warm = solve(db, xa)
    print(f"{b}: cold {cold[0]:.2f}s nodes {cold[3]} obj {cold[2]:.3f} | warm-from-{a} {warm[0]:.2f}s nodes {warm[3]} obj {warm[2]:.3f} {warm[4]}", flush=True)

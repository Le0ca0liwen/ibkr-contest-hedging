"""Greek-matched fixed-list hedge (no fitting to history), then the marginal value of scanned instruments on its residual."""
import sys, json
import os
from pathlib import Path
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ECON = RESEARCH_ROOT / "marketdata" / "econ"          # daily.csv, chains.csv, cboe/VIX*_History.csv
OUTDIR = RESEARCH_ROOT / "econ" / "out"; OUTDIR.mkdir(parents=True, exist_ok=True)
import numpy as np
from scipy.optimize import linprog
CAL = sys.argv[1] if len(sys.argv) > 1 else "1"
import os
CAL = CAL + os.environ.get("TAG", "")
z = np.load(OUTDIR / f"scen_{CAL}.npz", allow_pickle=True)
G = json.load(open(OUTDIR / f"greeks_{CAL}.json"))
X, names, hs = z["X"], [str(n) for n in z["names"]], z["hs"]
B = {s: z[f"book_{s}"] for s in ["SPCX", "RKLB", "SPY"]}
N = len(z["book"]); col = lambda n: X[:, names.index(n)]
gre = {k: np.array(v) for k, v in G["GRE"].items()}; MG = {k: np.array(v) for k, v in G["MANDG"].items()}
st = lambda e: gre[f"SPY {e} 770C"] + gre[f"SPY {e} 770P"]
stp = lambda e: col(f"SPY {e} 770C") + col(f"SPY {e} 770P")
syn = lambda e: col(f"SPY {e} 770C") - col(f"SPY {e} 770P"); synd = lambda e: gre[f"SPY {e} 770C"][0] - gre[f"SPY {e} 770P"][0]
ea = lambda x: float(np.mean(np.abs(x)))
print("mandated greeks (delta sh, gamma sh/$, vega $/pt, theta $/day):", {k: v.round(1).tolist() for k, v in MG.items()})
res = {}
# --- SPY: gamma+vega matched with two neighbouring expiries, residual delta via synthetic (same symbol, app delta cap)
def spy_hedge(e1, e2, delta_via="syn"):
    A = np.array([[st(e1)[1], st(e2)[1]], [st(e1)[2], st(e2)[2]]]); a, b = np.linalg.solve(A, -MG["SPY"][1:3])
    pnl = B["SPY"] + a * stp(e1) + b * stp(e2)
    d = MG["SPY"][0] + a * st(e1)[0] + b * st(e2)[0]
    if delta_via == "syn":
        k = -d / synd("1127"); pnl = pnl + k * syn("1127")
    else:
        k = -d / 1.0035; pnl = pnl + k * col("IVV sh")
    theta = MG["SPY"][3] + a * st(e1)[3] + b * st(e2)[3]
    return pnl, dict(straddles={e1: round(a, 1), e2: round(b, 1)}, delta_hedge=round(k, 1), residual_theta=round(theta, 0))
print("SPY unhedged E|.| %.0f" % ea(B["SPY"]))
d_only = B["SPY"] + (-MG["SPY"][0] / synd("1127")) * syn("1127")
print("SPY delta-only (Nov27 synthetic) E|.| %.0f" % ea(d_only))
k27 = -MG["SPY"][2] / st("1127")[2]; v_only = B["SPY"] + k27 * stp("1127"); dd = MG["SPY"][0] + k27 * st("1127")[0]
v_only = v_only + (-dd / synd("1127")) * syn("1127")
print("SPY vega-matched Nov27 straddles only (%.1f) + delta E|.| %.0f" % (k27, ea(v_only)))
for e1, e2 in [("1113", "1127"), ("1113", "1218"), ("1127", "1218")]:
    p, info = spy_hedge(e1, e2); res[f"SPY {e1}/{e2}"] = p
    print(f"SPY gamma+vega via {e1}/{e2}: E|.| {ea(p):.0f}  sd {p.std():.0f}  {info}")
spy_best = res["SPY 1113/1127"]
# --- SPCX: shares only vs gamma+vega via Nov20 140C/150C + shares
sp_sh = B["SPCX"] + (-MG["SPCX"][0]) * col("SPCX sh")
A = np.array([[gre["SPCX 1120 140C"][1], gre["SPCX 1120 150C"][1]], [gre["SPCX 1120 140C"][2], gre["SPCX 1120 150C"][2]]])
a, b = np.linalg.solve(A, -MG["SPCX"][1:3]); d = MG["SPCX"][0] + a * gre["SPCX 1120 140C"][0] + b * gre["SPCX 1120 150C"][0]
sp_gv = B["SPCX"] + a * col("SPCX 1120 140C") + b * col("SPCX 1120 150C") - d * col("SPCX sh")
print(f"SPCX unhedged {ea(B['SPCX']):.0f}; shares-only ({-MG['SPCX'][0]:.0f} sh) {ea(sp_sh):.0f}; gamma+vega via 140C {a:.1f}/150C {b:.1f} + {-d:.0f} sh: {ea(sp_gv):.0f}")
# --- RKLB: shares only vs + 50P gamma
rk_sh = B["RKLB"] + (-MG["RKLB"][0]) * col("RKLB sh")
k = -MG["RKLB"][1] / gre["RKLB 1120 50P"][1]; d = MG["RKLB"][0] + k * gre["RKLB 1120 50P"][0]
rk_g = B["RKLB"] + k * col("RKLB 1120 50P") - d * col("RKLB sh")
print(f"RKLB unhedged {ea(B['RKLB']):.0f}; shares-only ({-MG['RKLB'][0]:.0f} sh) {ea(rk_sh):.0f}; gamma via 50P {k:.1f} + {-d:.0f} sh: {ea(rk_g):.0f}")
fixed = spy_best + sp_gv + rk_g
fixed_simple = spy_best + sp_sh + rk_sh
print(f"TOTAL unhedged {ea(z['book']):.0f}; fixed-list greek hedge {ea(fixed):.0f} (sd {fixed.std():.0f}, p95 {np.quantile(np.abs(fixed), .95):.0f}); simple (SPY calendar + own shares) {ea(fixed_simple):.0f}")
# --- XSP Nov20 replacement for the SPY calendar (spirit flag): vega-match with XSP straddles, delta via SPY Nov27 synthetic
xs = gre["XSP 1120 770C"] + gre["XSP 1120 770P"]; kx = -MG["SPY"][2] / xs[2]
px_ = B["SPY"] + kx * (col("XSP 1120 770C") + col("XSP 1120 770P")); dx = MG["SPY"][0] + kx * xs[0]
px_ = px_ + (-dx / synd("1127")) * syn("1127")
print(f"SPY via XSP Nov20 straddles ({kx:.1f}) + delta: E|.| {ea(px_):.0f}; total with SPCX/RKLB greek hedges {ea(px_ + sp_gv + rk_g):.0f}")
# --- marginal value of scanned instruments fitted (L1) on the fixed-list residual, 11-fold CV
peer_list = ["ASTS", "LUNR", "PL", "RDW", "BKSY", "KTOS", "ARKX", "UFO", "ITA", "XAR", "TSLA", "GOOGL", "QQQ", "IWM"]
def l1(Bv, A, bounds):
    n, k = A.shape; c = np.r_[np.zeros(k), np.ones(2 * n)]
    r = linprog(c, A_eq=np.hstack([A, -np.eye(n), np.eye(n)]), b_eq=-Bv, bounds=bounds + [(0, None)] * (2 * n), method="highs"); return r.x[:k]
def marginal(base, sel, label, bound):
    A = np.column_stack([col(n) for n in sel]); bounds = [bound(n) for n in sel]
    h = l1(base, A, bounds); ins = ea(base + A @ h)
    cv = np.empty(N)
    for f in np.array_split(np.arange(N), 11):
        tr = np.setdiff1d(np.arange(N), f); cv[f] = base[f] + A[f] @ l1(base[tr], A[tr], bounds)
    # round-trip half-spread cost of the fitted (full-sample) position, amortized as if turned over once per day
    cost = float(np.sum(np.abs(h) * np.array([hs[names.index(n)] for n in sel])))
    print(f"  + {label:<34} in-sample {ins:6.0f}  CV {ea(cv):6.0f}  (base {ea(base):.0f}; CV change {ea(cv) - ea(base):+.0f}/day; half-spread cost of fitted position ${cost:.0f})")
    return ea(cv) - ea(base)
px = {"ASTS": 41.8, "LUNR": 10.6, "PL": 13.4, "RDW": 8.0, "BKSY": 25.0, "KTOS": 70.0, "ARKX": 30.0, "UFO": 39.0, "ITA": 207.0, "XAR": 230.0, "TSLA": 374.0, "GOOGL": 250.0, "QQQ": 600.0, "IWM": 240.0, "IVV": 771.2}
sb = lambda n: (-1e5 / px[n.split()[0]], 1e5 / px[n.split()[0]]) if n.endswith("sh") else (-20, 20) if n.startswith("VX") else (-50, 50)
for base, lab in [(fixed, "fixed-list greek hedge"), (fixed_simple, "simple fixed list")]:
    print("Marginal value on", lab)
    marginal(base, [f"{p} sh" for p in peer_list], "14 space/defense/index peers", sb)
    for p in ["ARKX", "UFO", "TSLA", "ASTS", "ITA", "QQQ"]:
        marginal(base, [f"{p} sh"], p, sb)
    marginal(base, ["VX Nov fut"], "VIX Nov future", sb)
    marginal(base, ["XSP 1120 770C", "XSP 1120 770P"], "XSP Nov20 770 C/P (spirit flag)", sb)
    marginal(base, ["IVV sh"], "IVV", sb)
np.save(OUTDIR / f"fixed_resid_{CAL}.npy", fixed)

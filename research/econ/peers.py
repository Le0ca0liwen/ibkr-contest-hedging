import pandas as pd, numpy as np
import os
from pathlib import Path
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ECON = RESEARCH_ROOT / "marketdata" / "econ"          # daily.csv, chains.csv, cboe/VIX*_History.csv
OUTDIR = RESEARCH_ROOT / "econ" / "out"; OUTDIR.mkdir(parents=True, exist_ok=True)
d = pd.read_csv(ECON / "daily.csv")
p = d.pivot(index="date", columns="symbol", values="close").sort_index()
vix = pd.read_csv(ECON / "cboe" / "VIX_History.csv"); vix["date"] = pd.to_datetime(vix.DATE).dt.strftime("%Y-%m-%d")
p = p.join(vix.set_index("date").CLOSE.rename("VIX"))
r = p.drop(columns="VIX").pct_change(fill_method=None)
r["dVIX"] = p["VIX"].diff()
r = r.dropna(subset=["SPCX"]).dropna()
print("aligned returns with SPCX:", len(r), r.index[0], r.index[-1])
peers = ["SPY", "QQQ", "IWM", "RKLB", "ASTS", "LUNR", "PL", "RDW", "BKSY", "KTOS", "ARKX", "UFO", "ITA", "XAR", "TSLA", "GOOGL", "dVIX"]
for target in ["SPCX", "RKLB"]:
    rows = []
    for w in [len(r), 60, 20]:
        x = r.tail(w)
        # residual vs SPY
        b = x[target].cov(x["SPY"]) / x["SPY"].var()
        res = x[target] - b * x["SPY"]
        for s in peers:
            if s == target: continue
            bs = x[s].cov(x["SPY"]) / x["SPY"].var() if s != "SPY" else 1
            rs = x[s] - bs * x["SPY"]
            rows.append(dict(target=target, win=w, peer=s, corr=x[target].corr(x[s]), resid_corr_exSPY=res.corr(rs) if s != "SPY" else np.nan))
    t = pd.DataFrame(rows).pivot_table(index="peer", columns="win", values=["corr", "resid_corr_exSPY"]).round(2)
    print("==", target, "ann vol full/60/20:", [round(r[target].tail(w).std() * np.sqrt(252), 3) for w in [len(r), 60, 20]])
    print(t.sort_values(("corr", len(r)), ascending=False).to_string())
print("ann vols (full window):", (r.drop(columns="dVIX").std() * np.sqrt(252)).round(3).to_dict())
print("SPY-dVIX corr", r.SPY.corr(r.dVIX).round(3), "dVIX std", r.dVIX.std().round(3), "60d", r.dVIX.tail(60).std().round(3))
# best multi-peer hedge R^2 for SPCX and RKLB (in-sample and leave-one-out), excluding own shares
import itertools
def r2(y, X):
    X = np.column_stack([np.ones(len(y)), X]); beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta; return 1 - e.var() / y.var(), e
def loo_resid_std(y, X):
    X = np.column_stack([np.ones(len(y)), X]); H = X @ np.linalg.pinv(X.T @ X) @ X.T
    beta, *_ = np.linalg.lstsq(X, y, rcond=None); e = y - X @ beta
    return np.std(e / (1 - np.diag(H)))
pool = ["SPY", "QQQ", "IWM", "ASTS", "LUNR", "PL", "RDW", "BKSY", "KTOS", "ARKX", "UFO", "ITA", "XAR", "TSLA", "GOOGL"]
for target, other in [("SPCX", "RKLB"), ("RKLB", "SPCX")]:
    y = r[target].values
    print("==", target, "raw daily std %.4f" % y.std())
    for k in [1, 2, 3]:
        best = None
        for combo in itertools.combinations(pool + [other], k):
            R2, e = r2(y, r[list(combo)].values)
            if best is None or R2 > best[0]: best = (R2, combo, e.std(), loo_resid_std(y, r[list(combo)].values))
        print(f"best {k}-name hedge {best[1]}: in-sample R2 {best[0]:.3f}, resid std {best[2]:.4f} (={best[2]/y.std():.2f}x), LOO resid std {best[3]:.4f} (={best[3]/y.std():.2f}x)")
    for combo in [["SPY"], ["ARKX"], ["UFO"], ["SPY", "ARKX"], pool]:
        R2, e = r2(y, r[combo].values)
        print(f"  {'+'.join(combo) if len(combo)<5 else 'ALL 15 pool'}: R2 {R2:.3f} resid {e.std()/y.std():.2f}x, LOO {loo_resid_std(y, r[combo].values)/y.std():.2f}x")
r.to_csv(OUTDIR / "returns_aligned.csv")

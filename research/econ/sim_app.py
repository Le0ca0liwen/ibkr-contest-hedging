"""Fit hedges on APP-STYLE scenarios (scenarios.py:127-162: 64 antithetic draws, 60-session shrunk covariance,
per-symbol parallel IV shock 0.015*sqrt(days)*normals[:, j+1], seed 17), then score the fitted hedge on the 88
realized historical days (with term-structure IV moves).  Shows in-model gain vs realized gain from scanning."""
import sys, json
import os
from pathlib import Path
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ECON = RESEARCH_ROOT / "marketdata" / "econ"          # daily.csv, chains.csv, cboe/VIX*_History.csv
OUTDIR = RESEARCH_ROOT / "econ" / "out"; OUTDIR.mkdir(parents=True, exist_ok=True)
from datetime import date, datetime
from types import SimpleNamespace
import numpy as np, pandas as pd
from scipy.optimize import linprog
sys.path.insert(0, os.environ.get("HEDGE_SRC") or os.environ.get("REPO") or str(RESEARCH_ROOT.parent))  # study: 982eeed
from hedge.config import NY
from hedge.models import Contract
from hedge.valuation import ValuationEngine
T0 = datetime(2026, 10, 21, 16, tzinfo=NY); T1 = datetime(2026, 10, 22, 16, tzinfo=NY); DAYS = 1.0
val = ValuationEngine(SimpleNamespace(risk_free_rate=0.04, dividend_yields={}, dividends={}))
spot = {"SPCX": 147.24, "RKLB": 51.15, "SPY": 768.51}
D = lambda s: date(int(s[:4]), int(s[4:6]), int(s[6:]))
ret = pd.read_csv(OUTDIR / "returns_aligned.csv", index_col=0)
z = np.load(OUTDIR / "scen_1.npz", allow_pickle=True)
Xh, bookh, names = z["X"], z["book"], [str(n) for n in z["names"]]
peer_list = ["ASTS", "LUNR", "PL", "RDW", "BKSY", "KTOS", "ARKX", "UFO", "ITA", "XAR", "TSLA", "GOOGL", "QQQ", "IWM"]
peer_px = {"ASTS": 41.8, "LUNR": 10.6, "PL": 13.4, "RDW": 8.0, "BKSY": 25.0, "KTOS": 70.0, "ARKX": 30.0, "UFO": 39.0, "ITA": 207.0, "XAR": 230.0, "TSLA": 374.0, "GOOGL": 250.0, "QQQ": 600.0, "IWM": 240.0}
mand = [("SPCX", "20261120", 130, "C", 0.586, 25), ("SPCX", "20261120", 145, "C", 0.570, -50), ("SPCX", "20261120", 160, "C", 0.562, 25),
        ("RKLB", "20261120", 45, "P", 0.790, -25), ("RKLB", "20261120", 55, "P", 0.800, 25),
        ("SPY", "20261120", 770, "C", 0.136, 25), ("SPY", "20261120", 770, "P", 0.134, 25)]
own_opts = [("SPY", "20261113", 770, "C", 0.1328), ("SPY", "20261113", 770, "P", 0.1328), ("SPY", "20261127", 770, "C", 0.1321), ("SPY", "20261127", 770, "P", 0.1320),
            ("SPY", "20261218", 770, "C", 0.1380), ("SPY", "20261218", 770, "P", 0.1380), ("SPCX", "20261120", 135, "C", 0.581), ("SPCX", "20261120", 140, "C", 0.574),
            ("SPCX", "20261120", 150, "C", 0.568), ("SPCX", "20261120", 155, "C", 0.564), ("SPCX", "20261127", 145, "C", 0.546), ("SPCX", "20261218", 145, "C", 0.519),
            ("RKLB", "20261120", 50, "P", 0.779), ("RKLB", "20261218", 45, "P", 0.730), ("RKLB", "20261218", 55, "P", 0.719)]

SEED = 17
COSTS = True
def app_scenarios(universe_syms, end_date="2026-10-21"):
    symbols = sorted(universe_syms)
    rng = np.random.default_rng(SEED); n = 64
    normals = rng.standard_normal((n // 2, len(symbols) + 1)); normals = np.vstack([normals, -normals])
    r = ret[symbols].tail(60)
    emp = r.cov().to_numpy(); cov = 0.8 * emp + 0.2 * np.diag(np.diag(emp)); cov += np.eye(len(symbols)) * max(1e-10, np.trace(cov) * 1e-6)
    shocks = normals[:, 1:] @ np.linalg.cholesky(cov).T * np.sqrt(DAYS * 252 / 365)
    shocks = {s: shocks[:, j] for j, s in enumerate(symbols)}
    iv = {s: 0.015 * np.sqrt(DAYS) * normals[:, j + 1] for j, s in enumerate(symbols)}
    return shocks, iv, n
def opt_change(sym, e, K, rt, ivv, r, div):
    c = Contract(con_id=1, symbol=sym, kind="OPT", multiplier=100, expiry=D(e), strike=K, right=rt, underlying_id=9)
    p0 = val.price(c, spot[sym], ivv, T0)
    return np.array([100 * (val.price(c, spot[sym] * (1 + float(np.clip(r[t], -0.8, 2))), max(0.01, ivv + div[t]), T1) - p0) for t in range(len(r))])
def run(universe_syms, inst_names):
    sh, iv, n = app_scenarios(universe_syms)
    book = sum(q * opt_change(s, e, K, rt, v, sh[s], iv[s]) for s, e, K, rt, v, q in mand)
    cols = []
    for nm in inst_names:
        if nm.endswith(" sh"):
            s = nm.split()[0]; px = spot.get(s, peer_px.get(s, 771.2)); cols.append(px * sh[s])
        else:
            s, e, KR = nm.split(); spec = [o for o in own_opts if o[0] == s and o[1][4:] == e and f"{o[2]}{o[3]}" == KR][0]
            cols.append(opt_change(s, spec[1], spec[2], spec[3], spec[4], sh[s], iv[s]))
    A = np.column_stack(cols)
    bounds = []
    for nm in inst_names:
        if nm.endswith(" sh"):
            s = nm.split()[0]; px = spot.get(s, peer_px.get(s, 771.2)); bounds.append((-5e5 / px, 5e5 / px))
        else:
            bounds.append((-50, 50))
    k = A.shape[1]
    half = np.array([z["hs"][names.index(nm)] for nm in inst_names]) if COSTS else np.zeros(k)
    # variables: h = hp - hm (hp, hm >= 0), u, v; objective mean|resid| + sum(half * |h|)
    c = np.r_[half, half, np.ones(2 * n) / n]
    lo = [b[0] for b in bounds]; hi = [b[1] for b in bounds]
    bnds = [(0, -l) for l in lo] + [(0, h_) for h_ in hi]
    bnds = [(0, h_) for h_ in hi] + [(0, -l) for l in lo] + [(0, None)] * (2 * n)
    sol = linprog(c, A_eq=np.hstack([A, -A, -np.eye(n), np.eye(n)]), b_eq=-book, bounds=bnds, method="highs")
    h = sol.x[:k] - sol.x[k:2 * k]
    model = float(np.mean(np.abs(book + A @ h)))
    Ah = np.column_stack([Xh[:, names.index(nm)] for nm in inst_names])
    realized = bookh + Ah @ h
    return model, float(np.mean(np.abs(realized))), float(np.mean(np.abs(book))), {nm: round(float(x), 1) for nm, x in zip(inst_names, h) if abs(x) > 0.5}
fixed = ["SPCX sh", "RKLB sh", "IVV sh"] + [f"{s} {e[4:]} {K}{rt}" for s, e, K, rt, _ in own_opts]
peers = [f"{p} sh" for p in peer_list]

configs = [
    ("fixed list (SPCX,RKLB,SPY,IVV)", ["SPCX", "RKLB", "SPY", "IVV"], fixed),
    ("fixed + ARKX,UFO", ["SPCX", "RKLB", "SPY", "IVV", "ARKX", "UFO"], fixed + ["ARKX sh", "UFO sh"]),
    ("fixed + 8 scanned (12 underlyings)", ["SPCX", "RKLB", "SPY", "IVV", "ARKX", "UFO", "ASTS", "LUNR", "PL", "TSLA", "ITA", "QQQ"], fixed + [f"{p} sh" for p in ["ARKX", "UFO", "ASTS", "LUNR", "PL", "TSLA", "ITA", "QQQ"]]),
    ("fixed + all 14 scanned", ["SPCX", "RKLB", "SPY", "IVV"] + peer_list, fixed + peers),
]
import itertools
res = {c[0]: [] for c in configs}
for COSTS_ in (False, True):
    COSTS = COSTS_
    res = {c[0]: [] for c in configs}
    for SEED in range(1, 21):
        for label, syms, inst in configs:
            model, realized, unh, pos = run(syms, inst)
            peer_notional = sum(abs(v) * peer_px[k.split()[0]] for k, v in pos.items() if k.split()[0] in peer_px)
            res[label].append((model, realized, peer_notional))
    print("== costs in objective:", COSTS)
    base = np.array([r[1] for r in res[configs[0][0]]])
    for label, *_ in configs:
        a = np.array(res[label])
        print(f"{label:<38} app-model E|.| mean ${a[:,0].mean():6.0f}   realized E|.| mean ${a[:,1].mean():6.0f} (median {np.median(a[:,1]):.0f}; worse than fixed in {(a[:,1] > base + 1e-6).sum()}/20 seeds; mean change {np.mean(a[:,1]-base):+.0f})  mean peer notional ${a[:,2].mean():,.0f}")

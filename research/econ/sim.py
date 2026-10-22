"""Historical-scenario hedge study for the mandated book (scratch; read-only use of the app's ValuationEngine).

Scenarios: the 88 joint daily closes 2026-06-15..2026-10-19 (all days SPCX traded), one-session horizon
from Day 0 close (Wed Oct 21 16:00) to Oct 22 16:00 (n_i = 1).  SPY IV changes per expiry come from the
CBOE VIX9D/VIX/VIX3M term structure (log-maturity interpolation, x0.75 ATM-to-VIX ratio, +0.10 vol-pt
idiosyncratic noise per expiry).  SPCX/RKLB IV changes are modeled (-0.25 x own return + N(0, 1.5/2.0 pts),
parallel across their expiries + 0.5 pt per-expiry noise).
"""
import sys, json, math
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

T0 = datetime(2026, 10, 21, 16, tzinfo=NY)
CAL_DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
T1 = T0 + pd.Timedelta(days=CAL_DAYS).to_pytimedelta()
import os
ATM_RATIO, EXP_NOISE = float(os.environ.get('ATM_RATIO', 0.75)), float(os.environ.get('EXP_NOISE', 0.10))
TWIST = os.environ.get('TWIST', '1') == '1'
TAG = os.environ.get('TAG', '')
rng = np.random.default_rng(7)
val = ValuationEngine(SimpleNamespace(risk_free_rate=0.04, dividend_yields={}, dividends={}))
spot = {"SPCX": 147.24, "RKLB": 51.15, "SPY": 768.51, "XSP": 768.51 * 1.0036}
D = lambda s: date(int(s[:4]), int(s[4:6]), int(s[6:]))

# ---- scenario factors --------------------------------------------------------
ret = pd.read_csv(OUTDIR / "returns_aligned.csv", index_col=0)
vix = {}
for s in ["VIX9D", "VIX", "VIX3M", "VIX6M"]:
    x = pd.read_csv(ECON / "cboe" / f"{s}_History.csv")
    x.index = pd.to_datetime(x.DATE).dt.strftime("%Y-%m-%d"); vix[s] = x.CLOSE
vix = pd.DataFrame(vix).dropna()
lvl_prev = vix.shift(1).loc[ret.index]; lvl = vix.loc[ret.index]
dv = (lvl - lvl_prev)
N = len(ret)
TEN = np.array([9, 30, 93, 182.0])
def interp(levels, T):  # levels: (N,4) array of index levels; linear in log maturity
    lt, lT = np.log(TEN), np.log(max(T, 9.0))
    k = np.clip(np.searchsorted(lt, lT) - 1, 0, 2); w = (lT - lt[k]) / (lt[k + 1] - lt[k])
    return levels[:, k] * (1 - w) + levels[:, k + 1] * w
def days_to(exp):
    return (datetime.combine(exp, datetime.min.time()).replace(hour=16, tzinfo=NY) - T0).total_seconds() / 86400
spy_exp = {e: days_to(D(e)) for e in ["20261113", "20261120", "20261127", "20261130", "20261218"]}
dIV_spy = {e: ATM_RATIO * (interp(dv.values, T) if TWIST else dv['VIX'].values) / 100 + rng.normal(0, EXP_NOISE / 100, N) for e, T in spy_exp.items()}
dIV_spy["XSP20261120"] = dIV_spy["20261120"] + rng.normal(0, 0.05 / 100, N)  # same expiry, other venue
# VIX Nov future (exp Nov 18): forward vol over [29d, 59d] from the CBOE term structure (index points)
def fwd(levels):
    s1, s2 = interp(levels, 28.0), interp(levels, 58.0)
    return np.sqrt(np.maximum((s2 ** 2 * 58 - s1 ** 2 * 28) / 30, 1))
dVXF = fwd(lvl.values) - fwd(lvl_prev.values)
iv_idio = {"SPCX": (0.25, 0.015), "RKLB": (0.25, 0.020)}
dIV_own = {s: -a * ret[s].values + rng.normal(0, b, N) for s, (a, b) in iv_idio.items()}
xsp_track = ret["SPY"].values + rng.normal(0, 0.00015, N)

# ---- instruments -------------------------------------------------------------
# (symbol, expiry, strike, right, iv, half_spread_per_share)
mand = [("SPCX", "20261120", 130, "C", 0.586, 25), ("SPCX", "20261120", 145, "C", 0.570, -50), ("SPCX", "20261120", 160, "C", 0.562, 25),
        ("RKLB", "20261120", 45, "P", 0.790, -25), ("RKLB", "20261120", 55, "P", 0.800, 25),
        ("SPY", "20261120", 770, "C", 0.136, 25), ("SPY", "20261120", 770, "P", 0.134, 25)]
own_opts = [  # allowed own-underlying options (not mandated, not forbidden)
    ("SPY", "20261113", 770, "C", 0.1328, 0.025), ("SPY", "20261113", 770, "P", 0.1328, 0.02),
    ("SPY", "20261127", 770, "C", 0.1321, 0.03), ("SPY", "20261127", 770, "P", 0.1320, 0.03),
    ("SPY", "20261218", 770, "C", 0.1380, 0.035), ("SPY", "20261218", 770, "P", 0.1380, 0.025),
    ("SPCX", "20261120", 135, "C", 0.581, 0.15), ("SPCX", "20261120", 140, "C", 0.574, 0.075), ("SPCX", "20261120", 150, "C", 0.568, 0.075),
    ("SPCX", "20261120", 155, "C", 0.564, 0.15), ("SPCX", "20261127", 145, "C", 0.546, 0.225), ("SPCX", "20261218", 145, "C", 0.519, 0.125),
    ("RKLB", "20261120", 50, "P", 0.779, 0.075), ("RKLB", "20261218", 45, "P", 0.730, 0.095), ("RKLB", "20261218", 55, "P", 0.719, 0.125),
]
xsp_opts = [("XSP", "20261120", 770, "C", 0.136, 0.045), ("XSP", "20261120", 770, "P", 0.1385, 0.035)]
def opt_pnl(sym, e, K, right, iv):
    c = Contract(con_id=1, symbol=sym, kind="OPT", multiplier=100, expiry=D(e), strike=K, right=right, underlying_id=9)
    S = spot[sym]
    if sym in ("SPCX", "RKLB"):
        r, div = ret[sym].values, dIV_own[sym] + rng.normal(0, 0.005, N) * (e != "20261120")
    elif sym == "SPY":
        r, div = ret["SPY"].values, dIV_spy[e]
    else:
        r, div = xsp_track, dIV_spy["XSP20261120"]
    p0 = val.price(c, S, iv, T0)
    return np.array([100 * (val.price(c, S * (1 + r[t]), max(0.01, iv + div[t]), T1) - p0) for t in range(N)])

book_by = {}
for s, e, K, rt, iv, q in mand:
    book_by[s] = book_by.get(s, 0) + q * opt_pnl(s, e, K, rt, iv)
book = sum(book_by.values())
def opt_greeks(sym, e, K, right, iv):
    c = Contract(con_id=1, symbol=sym, kind="OPT", multiplier=100, expiry=D(e), strike=K, right=right, underlying_id=9)
    x = val.greeks(c, spot[sym], iv, T0); return [100 * x["delta"], 100 * x["gamma"], x["vega"], 100 * x["theta"]]
GRE = {f"{s} {e[4:]} {K}{rt}": opt_greeks(s, e, K, rt, iv) for s, e, K, rt, iv, *_ in own_opts + xsp_opts}
MANDG = {}
for s, e, K, rt, iv, q in mand:
    MANDG[s] = [a + q * b for a, b in zip(MANDG.get(s, [0, 0, 0, 0]), opt_greeks(s, e, K, rt, iv))]
cols, names, hs = [], [], []
def add(name, pnl, half):
    names.append(name); cols.append(pnl); hs.append(half)
for s in ["SPCX", "RKLB"]:
    add(f"{s} sh", spot[s] * ret[s].values, 0.0002 * spot[s])
add("IVV sh", 768.51 * 1.0035 * ret["IVV"].values, 0.0001 * 770)
for s, e, K, rt, iv, h in own_opts:
    add(f"{s} {e[4:]} {K}{rt}", opt_pnl(s, e, K, rt, iv), 100 * h)
peer_list = ["ASTS", "LUNR", "PL", "RDW", "BKSY", "KTOS", "ARKX", "UFO", "ITA", "XAR", "TSLA", "GOOGL", "QQQ", "IWM"]
peer_px = {"ASTS": 41.8, "LUNR": 10.6, "PL": 13.4, "RDW": 8.0, "BKSY": 25.0, "KTOS": 70.0, "ARKX": 30.0, "UFO": 39.0, "ITA": 207.0,
           "XAR": 230.0, "TSLA": 374.0, "GOOGL": 250.0, "QQQ": 600.0, "IWM": 240.0}
peer_half_bps = {"ASTS": 5, "LUNR": 5, "PL": 4, "RDW": 6, "BKSY": 5, "KTOS": 3, "ARKX": 10, "UFO": 15, "ITA": 4, "XAR": 5, "TSLA": 0.5, "GOOGL": 0.5, "QQQ": 0.2, "IWM": 0.3}
for p in peer_list:
    add(f"{p} sh", peer_px[p] * ret[p].values, peer_px[p] * peer_half_bps[p] / 1e4)
add("VX Nov fut", 1000 * dVXF, 0.025 * 1000)
for s, e, K, rt, iv, h in xsp_opts:
    add(f"{s} {e[4:]} {K}{rt}", opt_pnl(s, e, K, rt, iv), 100 * h)
X = np.column_stack(cols); names = np.array(names); hs = np.array(hs)
np.savez(str(OUTDIR / ("scen_%g%s.npz" % (CAL_DAYS, TAG))), X=X, book=book, names=names, hs=hs, dates=np.array(ret.index),
         book_SPCX=book_by["SPCX"], book_RKLB=book_by["RKLB"], book_SPY=book_by["SPY"])
json.dump({"GRE": GRE, "MANDG": MANDG}, open(OUTDIR / ("greeks_%g%s.json" % (CAL_DAYS, TAG)), "w"))

def l1_fit(B, A):
    n, k = A.shape  # min sum(u+v) s.t. A h + B = u - v
    c = np.r_[np.zeros(k), np.ones(2 * n)]
    Aeq = np.hstack([A, -np.eye(n), np.eye(n)])
    # App caps: <=50 contracts per option (config.py:25); stock notional <= 0.5 x $1M NAV per symbol (config.py:23).
    px = {"SPCX": 147.24, "RKLB": 51.15, "IVV": 771.2}
    px.update(peer_px)
    bounds = [(-50, 50) if not nm.endswith("sh") else (-500000 / px[nm.split()[0]], 500000 / px[nm.split()[0]]) for nm in sel_names]
    bounds = [(-20, 20) if nm.startswith("VX") else b for nm, b in zip(sel_names, bounds)] + [(0, None)] * (2 * n)
    r = linprog(c, A_eq=Aeq, b_eq=-B, bounds=bounds, method="highs")
    return r.x[:k]
def evaluate(mask, label):
    global sel_names
    A = X[:, mask]; sel_names = names[mask]
    if A.shape[1] == 0:
        res = book; h = np.array([])
        cv = book
    else:
        h = l1_fit(book, A); res = book + A @ h
        folds = np.array_split(np.arange(N), 11); cv = np.empty(N)
        for f in folds:
            tr = np.setdiff1d(np.arange(N), f); hh = l1_fit(book[tr], A[tr]); cv[f] = book[f] + A[f] @ hh
    return dict(set=label, k=int(mask.sum()), insample_mean_abs=float(np.mean(np.abs(res))), cv_mean_abs=float(np.mean(np.abs(cv))),
                cv_std=float(np.std(cv)), cv_p95_abs=float(np.quantile(np.abs(cv), 0.95)), positions={n: round(float(x), 1) for n, x in zip(names[mask], h) if abs(x) > 0.5})
is_ = lambda pred: np.array([pred(n) for n in names])
own_sh = is_(lambda n: n in ("SPCX sh", "RKLB sh"))
spy_opt = is_(lambda n: n.startswith("SPY "))
spy_delta_only = is_(lambda n: n in ("SPY 1127 770C", "SPY 1127 770P"))
own_opt = is_(lambda n: n.startswith("SPCX 1") or n.startswith("RKLB 1"))
ivv = is_(lambda n: n == "IVV sh")
peers = is_(lambda n: n.split()[0] in peer_list)
vx = is_(lambda n: n.startswith("VX"))
xsp = is_(lambda n: n.startswith("XSP"))
sets = [
    (np.zeros(len(names), bool), "S0 unhedged"),
    (own_sh | ivv, "S1 own shares + IVV (delta only)"),
    (own_sh | spy_delta_only, "S1b own shares + SPY Nov27 C/P"),
    (own_sh | ivv | spy_opt, "S2 S1 + SPY Nov13/27/Dec18 options"),
    (own_sh | ivv | spy_opt | own_opt, "S3 fixed list: S2 + SPCX/RKLB options"),
    (own_sh | ivv | spy_opt | own_opt | peers, "S4 S3 + 14 scanned stocks/ETFs"),
    (own_sh | ivv | spy_opt | own_opt | vx, "S5 S3 + VIX Nov future"),
    (own_sh | ivv | spy_opt | own_opt | peers | vx, "S6 S3 + peers + VIX"),
    (own_sh | ivv | spy_opt | own_opt | xsp, "S7 S3 + XSP Nov20 770 (spirit flag)"),
    (own_sh | ivv | peers, "P1 peers instead of own options (IVV+own shares+peers)"),
    (ivv | peers, "P2 peers only, no own shares"),
    (ivv | spy_opt | vx, "V1 IVV + SPY options + VIX"),
    (ivv | own_sh | vx, "V2 own shares + IVV + VIX (no SPY options)"),
]
out = [evaluate(m, l) for m, l in sets]
for o in out:
    print(f"{o['set']:<58} k={o['k']:>2} in-sample E|.|=${o['insample_mean_abs']:>7,.0f}  CV E|.|=${o['cv_mean_abs']:>7,.0f}  CV sd=${o['cv_std']:>7,.0f}  CV p95|.|=${o['cv_p95_abs']:>7,.0f}")
json.dump(out, open(OUTDIR / ("sim_%g%s.json" % (CAL_DAYS, TAG)), "w"), indent=1)
print("book pnl sd", book.std().round(0), "mean", book.mean().round(0))
for o in out:
    if o["set"].startswith(("S3", "S4", "S5", "S7", "P1")):
        print(o["set"], o["positions"])

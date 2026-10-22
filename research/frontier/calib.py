"""Calibrate today's (2026-10-21 13:13 ET) IVs from the operator's monitor mids with the app's own
American model, compare with Oct-20 chains.csv IVs, and derive per-(symbol, expiry) scale factors
used to price strikes/expiries that are not on today's monitor.  Writes calib.json."""
import json
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

RESEARCH_ROOT = os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.environ.get("REPO") or os.path.dirname(RESEARCH_ROOT)
sys.path.insert(0, REPO)
from hedge.config import NY  # noqa: E402
from hedge.models import Contract  # noqa: E402
from hedge.valuation import ValuationEngine  # noqa: E402

NOW = datetime(2026, 10, 21, 13, 13, tzinfo=NY)
SPOT = {"SPCX": (160.30 + 160.32) / 2, "RKLB": (53.24 + 53.25) / 2, "SPY": (773.84 + 773.86) / 2}
# (symbol, expiry, strike, right): (bid, ask) from the operator's data monitor, 13:13 ET
MONITOR = {
    ("SPCX", "2026-11-20", 130, "C"): (31.45, 32.45),
    ("SPCX", "2026-11-20", 145, "C"): (18.95, 19.70),
    ("SPCX", "2026-11-20", 160, "C"): (9.90, 10.05),
    ("RKLB", "2026-11-20", 45, "P"): (1.37, 1.43),
    ("RKLB", "2026-11-20", 55, "P"): (5.45, 5.65),
    ("SPY", "2026-11-20", 770, "C"): (15.82, 15.90),
    ("SPY", "2026-11-20", 770, "P"): (9.28, 9.31),
    ("RKLB", "2026-11-20", 50, "P"): (2.97, 3.10),
    ("RKLB", "2026-11-27", 50, "C"): (6.30, 7.30),
    ("RKLB", "2026-11-27", 50, "P"): (3.15, 3.40),
    ("SPCX", "2026-11-20", 145, "P"): (3.50, 3.60),
    ("SPCX", "2026-11-27", 145, "C"): (19.50, 20.85),
    ("SPCX", "2026-11-27", 145, "P"): (3.90, 4.15),
    ("SPY", "2026-11-13", 770, "C"): (13.86, 13.98),
    ("SPY", "2026-11-13", 770, "P"): (7.94, 7.98),
    ("SPY", "2026-11-27", 770, "C"): (17.24, 17.43),
    ("SPY", "2026-11-27", 770, "P"): (10.14, 10.18),
}

val = ValuationEngine(SimpleNamespace(risk_free_rate=0.04, dividend_yields={}, dividends={}))
chains = pd.read_csv(os.path.join(RESEARCH_ROOT, "marketdata", "econ", "chains.csv"))


def chain_row(sym, exp, k, r):
    x = chains[(chains.root == sym) & (chains.expiry == int(exp.replace("-", ""))) & (chains.strike == k) & (chains.right == r)]
    return None if x.empty else x.iloc[0]


def contract(sym, exp, k, r):
    return Contract(con_id=1, symbol=sym, kind="OPT", multiplier=100, expiry=date.fromisoformat(exp), strike=float(k), right=r, underlying_id=9)


rows = []
for (sym, exp, k, r), (bid, ask) in MONITOR.items():
    c = contract(sym, exp, k, r)
    mid = (bid + ask) / 2
    iv = val.implied_volatility(c, SPOT[sym], mid, NOW)
    ch = chain_row(sym, exp, k, r)
    # Chain IV re-derived with the app model from the Oct-20 chain mid and its own underlying (so the
    # ratio compares like with like rather than Moomoo's vendor IV).
    ch_under = float(ch.under)
    ch_at = datetime(2026, 10, 20, 15, 0, tzinfo=NY)
    try:
        ch_iv_model = val.implied_volatility(c, ch_under, (ch.bid + ch.ask) / 2, ch_at)
    except Exception:
        ch_iv_model = float("nan")
    rows.append(dict(sym=sym, exp=exp, k=k, r=r, bid=bid, ask=ask, mid=mid, spread=round(ask - bid, 2),
                     iv_today=round(iv, 4), chain_iv_vendor=float(ch.iv), chain_iv_model=round(ch_iv_model, 4),
                     ratio_vendor=round(iv / float(ch.iv), 4), ratio_model=round(iv / ch_iv_model, 4)))
df = pd.DataFrame(rows)
pd.set_option("display.width", 220)
print(df.to_string())
# Per (symbol, expiry) scale = median(today IV / chain vendor IV) over that expiry's monitor contracts
scale = {f"{s}|{e}": float(g.ratio_vendor.median()) for (s, e), g in df.groupby(["sym", "exp"])}
print(json.dumps(scale, indent=1))
json.dump({"now": NOW.isoformat(), "spot": SPOT, "monitor": {"|".join(map(str, key)): v for key, v in MONITOR.items()},
           "iv_today": {f"{r.sym}|{r.exp}|{r.k}|{r.r}": r.iv_today for r in df.itertuples()}, "scale": scale},
          open(os.path.join(RESEARCH_ROOT, "frontier", "calib.json"), "w"), indent=1)

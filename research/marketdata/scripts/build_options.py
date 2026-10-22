"""Option inputs for the real-data replay: chain extract, today's quotes, IV scaling, spreads.

Usage: python -I build_options.py OUT_DIR

Inputs
  * marketdata/econ/chains.csv  -- CBOE delayed chain snapshot pulled 2026-10-20 21:10 UTC
    (17:10 ET). Option bid/ask are the Oct-20 CLOSING quotes; the file's `under` column is an
    AFTER-HOURS stock print (SPCX 153.67 vs official close 147.79), so it is NOT the spot the option
    quotes reflect. We recover that spot from put-call parity (it matches the official close).
  * today's quotes: operator monitor 2026-10-21 13:13 ET (same table as
    MONITOR in research/frontier/calib.py), spots SPCX 160.30 / RKLB 53.24 / SPY 773.85,
    checked against the 13:13 one-minute bars.
Pricing: the app's own hedge.valuation.ValuationEngine (American, Bjerksund-Stensland, r=0.04, no
dividends -- config/contest.template.yaml values).
"""

import json
import math
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

# research/ root (env RESEARCH_ROOT, default: two levels above this script) and the app source (env REPO,
# default: the repository root above research/).
RESEARCH_ROOT = os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REPO = os.environ.get("REPO") or os.path.dirname(RESEARCH_ROOT)
sys.path.insert(0, REPO)
from hedge.config import NY  # noqa: E402
from hedge.models import Contract  # noqa: E402
from hedge.valuation import BelowModelFloor, ValuationEngine  # noqa: E402

OUT = sys.argv[1]
CHAINS = os.path.join(RESEARCH_ROOT, "marketdata", "econ", "chains.csv")
R = 0.04
val = ValuationEngine(SimpleNamespace(risk_free_rate=R, dividend_yields={}, dividends={}))
CHAIN_AT = datetime(2026, 10, 20, 16, 0, tzinfo=NY)  # option closing quotes
TODAY_AT = datetime(2026, 10, 21, 13, 13, tzinfo=NY)
TODAY_SPOT = {"SPCX": 160.30, "RKLB": 53.24, "SPY": 773.85}
sessions = pd.read_csv(os.path.join(OUT, "sessions_summary.csv"))
OFFICIAL_1020 = {r.symbol: r.official_close for r in sessions[sessions.date == "2026-10-20"].itertuples()}

MANDATED = {("SPCX", 20261120, 130.0, "C"): 25, ("SPCX", 20261120, 145.0, "C"): -50,
            ("SPCX", 20261120, 160.0, "C"): 25, ("RKLB", 20261120, 45.0, "P"): -25,
            ("RKLB", 20261120, 55.0, "P"): 25, ("SPY", 20261120, 770.0, "C"): 25, ("SPY", 20261120, 770.0, "P"): 25}
HEDGE = {("RKLB", 20261120, 50.0, "P"), ("RKLB", 20261127, 50.0, "C"), ("RKLB", 20261127, 50.0, "P"),
         ("SPCX", 20261120, 145.0, "P"), ("SPCX", 20261127, 145.0, "C"), ("SPCX", 20261127, 145.0, "P"),
         ("SPY", 20261113, 770.0, "C"), ("SPY", 20261113, 770.0, "P"), ("SPY", 20261127, 770.0, "C"),
         ("SPY", 20261127, 770.0, "P")}
# today's monitor quotes (bid, ask) at 13:13 ET
TODAY = {
    ("SPCX", 20261120, 130.0, "C"): (31.45, 32.45), ("SPCX", 20261120, 145.0, "C"): (18.95, 19.70),
    ("SPCX", 20261120, 160.0, "C"): (9.90, 10.05), ("RKLB", 20261120, 45.0, "P"): (1.37, 1.43),
    ("RKLB", 20261120, 55.0, "P"): (5.45, 5.65), ("SPY", 20261120, 770.0, "C"): (15.82, 15.90),
    ("SPY", 20261120, 770.0, "P"): (9.28, 9.31), ("RKLB", 20261120, 50.0, "P"): (2.97, 3.10),
    ("RKLB", 20261127, 50.0, "C"): (6.30, 7.30), ("RKLB", 20261127, 50.0, "P"): (3.15, 3.40),
    ("SPCX", 20261120, 145.0, "P"): (3.50, 3.60), ("SPCX", 20261127, 145.0, "C"): (19.50, 20.85),
    ("SPCX", 20261127, 145.0, "P"): (3.90, 4.15), ("SPY", 20261113, 770.0, "C"): (13.86, 13.98),
    ("SPY", 20261113, 770.0, "P"): (7.94, 7.98), ("SPY", 20261127, 770.0, "C"): (17.24, 17.43),
    ("SPY", 20261127, 770.0, "P"): (10.14, 10.18),
}
# extraction window (superset of the requested nearby strikes)
WINDOW = {("SPCX", 20261113): (130, 170), ("SPCX", 20261120): (125, 175), ("SPCX", 20261127): (125, 175),
          ("RKLB", 20261120): (40, 60), ("RKLB", 20261127): (40, 60),
          ("SPY", 20261113): (760, 785), ("SPY", 20261120): (760, 785), ("SPY", 20261127): (760, 785)}
REQUESTED_NEARBY = {("SPCX", 20261120): (150, 170), ("SPCX", 20261127): (150, 170),
                    ("RKLB", 20261120): (50, 55), ("RKLB", 20261127): (50, 55),
                    ("SPY", 20261113): (765, 780), ("SPY", 20261127): (765, 780)}


def D(e):
    e = str(e)
    return date(int(e[:4]), int(e[4:6]), int(e[6:]))


def contract(sym, e, K, rt):
    return Contract(con_id=1, symbol=sym, kind="OPT", multiplier=100, expiry=D(e), strike=float(K), right=rt,
                    underlying_id=9)


def iv_of(sym, e, K, rt, spot, mid, at):
    try:
        return val.implied_volatility(contract(sym, e, K, rt), spot, mid, at), ""
    except BelowModelFloor:
        return np.nan, "below_model_floor"
    except ValueError as ex:
        return np.nan, str(ex)[:40]


def tradable(sym, e, rt):
    if sym == "RKLB" and e == 20261120 and rt == "C":
        return 0  # forbidden: Nov20 RKLB calls
    return 1


chains = pd.read_csv(CHAINS)
rows = []
for (sym, e), (lo, hi) in WINDOW.items():
    q = chains[(chains.root == sym) & (chains.expiry == e) & (chains.strike >= lo) & (chains.strike <= hi)]
    rows.append(q)
sub = pd.concat(rows).drop(columns=["file"]).rename(columns={"root": "symbol", "iv": "iv_cboe", "delta": "delta_cboe",
                                                               "gamma": "gamma_cboe", "vega": "vega_cboe",
                                                               "theta": "theta_cboe", "under": "under_file"})
sub["mid"] = (sub.bid + sub.ask) / 2
sub["spread"] = sub.ask - sub.bid
sub["rel_spread"] = np.where(sub.mid > 0, sub.spread / sub.mid, np.nan)

# ---------------------------------------------------------------- parity-implied spot of the chain quotes
log = []
parity = {}
for (sym, e), g in sub.groupby(["symbol", "expiry"]):
    T = (datetime.combine(D(e), datetime.min.time(), NY).replace(hour=16) - CHAIN_AT).total_seconds() / (365 * 86400)
    c = g[g.right == "C"].set_index("strike")
    p = g[g.right == "P"].set_index("strike")
    ks = [k for k in c.index if k in p.index and c.loc[k, "bid"] > 0 and p.loc[k, "bid"] > 0]
    ref = OFFICIAL_1020[sym]
    ks = sorted(ks, key=lambda k: abs(k - ref))[:5]
    est = [c.loc[k, "mid"] - p.loc[k, "mid"] + k * math.exp(-R * T) for k in ks]
    parity[(sym, e)] = float(np.median(est))
for sym in ["SPCX", "RKLB", "SPY"]:
    v = [parity[k] for k in parity if k[0] == sym]
    log.append(f"chain parity-implied spot {sym}: " + ", ".join(f"{k[1]}={parity[k]:.2f}" for k in parity if k[0] == sym)
               + f" | official 10-20 close {OFFICIAL_1020[sym]:.2f} | file `under` {sub[sub.symbol == sym].under_file.iloc[0]:.2f}")
CHAIN_SPOT = {s: float(np.median([parity[k] for k in parity if k[0] == s])) for s in ["SPCX", "RKLB", "SPY"]}
log.append(f"chain reference spot used (median parity over expiries): {CHAIN_SPOT}")

# ---------------------------------------------------------------- app-engine IV on the chain quotes
ivs, notes = [], []
for r in sub.itertuples():
    if r.bid <= 0 or r.ask <= 0:
        ivs.append(np.nan)
        notes.append("no_bid")
        continue
    v, n = iv_of(r.symbol, r.expiry, r.strike, r.right, CHAIN_SPOT[r.symbol], r.mid, CHAIN_AT)
    ivs.append(v)
    notes.append(n)
sub["iv_app_chain"] = ivs
sub["iv_note"] = notes
sub["chain_spot"] = sub.symbol.map(CHAIN_SPOT)
sub["log_moneyness_chain"] = np.log(sub.strike / sub.chain_spot)

# smile per (symbol, expiry): quadratic in log-moneyness over OTM side (+/- 2.5% both), weighted by 1/spread
smiles = {}
for (sym, e), g in sub.groupby(["symbol", "expiry"]):
    x = g.log_moneyness_chain
    q = g[(g.iv_app_chain > 0.03) & (x.abs() < 0.25) &
          (((g.right == "C") & (x > -0.025)) | ((g.right == "P") & (x < 0.025)))]
    w = 1 / np.maximum(q.spread, 0.02)
    coef = np.polyfit(q.log_moneyness_chain, q.iv_app_chain, 2, w=w)
    resid = q.iv_app_chain - np.polyval(coef, q.log_moneyness_chain)
    smiles[(sym, e)] = dict(coef=coef.tolist(), n=len(q), rmse=float(np.sqrt(np.mean(resid**2))))

# ---------------------------------------------------------------- today's quotes -> IV, ratio
trows = []
for (sym, e, K, rt), (b, a) in TODAY.items():
    mid = (b + a) / 2
    v, n = iv_of(sym, e, K, rt, TODAY_SPOT[sym], mid, TODAY_AT)
    x = math.log(K / TODAY_SPOT[sym])
    smile_iv = float(np.polyval(smiles[(sym, e)]["coef"], x))
    g = val.greeks(contract(sym, e, K, rt), TODAY_SPOT[sym], v, TODAY_AT)
    ch = sub[(sub.symbol == sym) & (sub.expiry == e) & (sub.strike == K) & (sub.right == rt)]
    trows.append(dict(symbol=sym, expiry=e, strike=K, right=rt, bid=b, ask=a, mid=mid, spread=round(a - b, 4),
                      rel_spread=(a - b) / mid, spot=TODAY_SPOT[sym], quoted_at=TODAY_AT.isoformat(), iv_today=v,
                      iv_note=n, chain_smile_iv_sticky_moneyness=smile_iv, ratio=v / smile_iv,
                      chain_bid=float(ch.bid.iloc[0]) if len(ch) else np.nan,
                      chain_ask=float(ch.ask.iloc[0]) if len(ch) else np.nan,
                      chain_rel_spread=float(ch.rel_spread.iloc[0]) if len(ch) else np.nan,
                      chain_iv_app=float(ch.iv_app_chain.iloc[0]) if len(ch) else np.nan,
                      **{k: g[k] for k in ("delta", "gamma", "vega", "theta")}))
today = pd.DataFrame(trows)
ratio = today.groupby(["symbol", "expiry"]).ratio.median().to_dict()
for sym in ["SPCX", "RKLB", "SPY"]:
    for e in [20261113, 20261120, 20261127]:
        if (sym, e) in smiles and (sym, e) not in ratio:
            ratio[(sym, e)] = ratio[(sym, 20261120)]
            log.append(f"no quote today for {sym} {e}: IV ratio borrowed from {sym} 20261120")
log.append("today/chain IV ratio (sticky moneyness, median over quoted contracts): "
           + ", ".join(f"{k[0]} {k[1]}={v:.3f}" for k, v in sorted(ratio.items())))
today.to_csv(os.path.join(OUT, "options_today_quotes.csv"), index=False, float_format="%.6g")

# ---------------------------------------------------------------- sim-ready contract table
crow = []
for r in sub.itertuples():
    key = (r.symbol, r.expiry, float(r.strike), r.right)
    x_today = math.log(r.strike / TODAY_SPOT[r.symbol])
    sm = smiles[(r.symbol, r.expiry)]
    iv_scaled = float(np.polyval(sm["coef"], x_today)) * ratio[(r.symbol, r.expiry)]
    tq = TODAY.get(key)
    if tq is not None:
        tv = today[(today.symbol == r.symbol) & (today.expiry == r.expiry) & (today.strike == r.strike) & (today.right == r.right)]
        iv_sim, iv_src = float(tv.iv_today.iloc[0]), "today_quote"
        rs_today = (tq[1] - tq[0]) / ((tq[0] + tq[1]) / 2)
    else:
        iv_sim, iv_src, rs_today = iv_scaled, "chain_smile_x_ratio", np.nan
    lo, hi = REQUESTED_NEARBY.get((r.symbol, r.expiry), (np.inf, -np.inf))
    role = "mandated" if key in MANDATED else "hedge" if key in HEDGE else (
        "nearby_requested" if lo <= r.strike <= hi else "nearby_extra")
    is_trad = tradable(r.symbol, r.expiry, r.right) and not (r.symbol == "SPY" and r.expiry == 20261120)
    c = contract(r.symbol, r.expiry, r.strike, r.right)
    p_today = val.price(c, TODAY_SPOT[r.symbol], iv_sim, TODAY_AT)
    g = val.greeks(c, TODAY_SPOT[r.symbol], iv_sim, TODAY_AT)
    # spread to apply in the sim: today's observed relative spread where quoted, else the chain's
    if not np.isnan(rs_today):
        rs_sim, rs_src = rs_today, "today_quote"
    elif r.bid > 0 and not np.isnan(r.rel_spread):
        rs_sim, rs_src = float(r.rel_spread), "chain"
    else:
        rs_sim, rs_src = np.nan, "none"
    crow.append(dict(
        name=f"{r.symbol} {str(r.expiry)[4:]} {r.strike:g}{r.right}", symbol=r.symbol, expiry=r.expiry,
        strike=r.strike, right=r.right, role=role, mandated_qty=MANDATED.get(key, 0),
        in_app_hedge_universe=int(key in HEDGE), contest_tradable=int(is_trad and key not in MANDATED),
        chain_bid=r.bid, chain_ask=r.ask, chain_mid=r.mid, chain_spread=round(r.spread, 4), chain_rel_spread=r.rel_spread,
        chain_iv_cboe=r.iv_cboe, chain_iv_app=r.iv_app_chain, chain_iv_note=r.iv_note, chain_oi=r.oi,
        chain_volume=r.volume,
        today_bid=tq[0] if tq else np.nan, today_ask=tq[1] if tq else np.nan, today_rel_spread=rs_today,
        iv_ratio=ratio[(r.symbol, r.expiry)], iv_scaled=iv_scaled, iv_sim=iv_sim, iv_sim_source=iv_src,
        rel_spread_sim=rs_sim, rel_spread_sim_source=rs_src,
        model_mid_today=p_today, model_half_spread_today=0.5 * rs_sim * p_today if rs_sim == rs_sim else np.nan,
        delta_today=g["delta"], gamma_today=g["gamma"], vega_today=g["vega"], theta_today=g["theta"]))
con = pd.DataFrame(crow).sort_values(["symbol", "expiry", "right", "strike"])
con.to_csv(os.path.join(OUT, "options_contracts.csv"), index=False, float_format="%.6g")
sub.sort_values(["symbol", "expiry", "right", "strike"]).to_csv(os.path.join(OUT, "options_chain_subset.csv"),
                                                                  index=False, float_format="%.6g")
json.dump(dict(chain_at=CHAIN_AT.isoformat(), chain_spot=CHAIN_SPOT, today_at=TODAY_AT.isoformat(),
               today_spot=TODAY_SPOT, risk_free_rate=R,
               smiles={f"{k[0]} {k[1]}": dict(v, ratio_today=ratio[k],
                                                note="iv(x)=polyval(coef, x)*ratio_today, x=ln(K/S)") for k, v in smiles.items()},
               parity_spot={f"{k[0]} {k[1]}": v for k, v in parity.items()}),
          open(os.path.join(OUT, "options_smiles.json"), "w"), indent=1)

# ---------------------------------------------------------------- report
pd.set_option("display.width", 250)
log.append("\nsmile fits (quadratic in ln(K/S), app-engine IV of chain mids):")
for k, v in smiles.items():
    log.append(f"  {k[0]} {k[1]}: n={v['n']} rmse={v['rmse']:.4f} coef={[round(c, 4) for c in v['coef']]}")
log.append("\ntoday's quotes (13:13 ET) vs chain (Oct-20 close):")
log.append(today[["symbol", "expiry", "strike", "right", "bid", "ask", "rel_spread", "chain_bid", "chain_ask",
                  "chain_rel_spread", "iv_today", "chain_iv_app", "ratio"]].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
key = con[con.role.isin(["mandated", "hedge"])]
log.append("\nmandated + hedge-universe contracts, sim inputs:")
log.append(key[["name", "role", "mandated_qty", "chain_rel_spread", "today_rel_spread", "rel_spread_sim", "iv_sim",
                "iv_sim_source", "model_mid_today", "delta_today", "vega_today"]].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
nb = con[con.role == "nearby_requested"]
log.append("\nrequested nearby strikes (sim inputs):")
log.append(nb[["name", "contest_tradable", "chain_bid", "chain_ask", "chain_rel_spread", "chain_iv_app", "iv_sim",
               "model_mid_today", "delta_today"]].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
print("\n".join(log))
open(os.path.join(OUT, "options_report.txt"), "w").write("\n".join(log) + "\n")

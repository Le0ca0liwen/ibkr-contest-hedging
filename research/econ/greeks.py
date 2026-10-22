"""Mandated-book greeks with the app's ValuationEngine (branch 982eeed, read-only import)."""
import sys, json
import os
from pathlib import Path
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ECON = RESEARCH_ROOT / "marketdata" / "econ"          # daily.csv, chains.csv, cboe/VIX*_History.csv
OUTDIR = RESEARCH_ROOT / "econ" / "out"; OUTDIR.mkdir(parents=True, exist_ok=True)
from datetime import date, datetime, time
from types import SimpleNamespace
sys.path.insert(0, os.environ.get("HEDGE_SRC") or os.environ.get("REPO") or str(RESEARCH_ROOT.parent))  # study: 982eeed
from hedge.config import NY
from hedge.models import Contract
from hedge.valuation import ValuationEngine

EXP = date(2026, 11, 20)
# App defaults: risk_free_rate 0.04 (config.py:143), no dividend yields configured in the contest template.
m = SimpleNamespace(risk_free_rate=0.04, dividend_yields={}, dividends={})
v = ValuationEngine(m)
spot = {"SPCX": 147.24, "RKLB": 51.15, "SPY": 768.51}
# Given IVs: SPY 770C 13.6, 770P 13.4, SPCX 145C 57, RKLB 45P 79. Others: CBOE Oct-20 chain skew offsets.
legs = [
    ("SPCX", "C", 130, 25, 0.586), ("SPCX", "C", 145, -50, 0.570), ("SPCX", "C", 160, 25, 0.562),
    ("RKLB", "P", 45, -25, 0.790), ("RKLB", "P", 55, 25, 0.800),
    ("SPY", "C", 770, 25, 0.136), ("SPY", "P", 770, 25, 0.134),
]
def greeks_at(at, spots=spot):
    rows, tot = [], {}
    for k, (s, r, K, q, iv) in enumerate(legs):
        c = Contract(con_id=k + 1, symbol=s, kind="OPT", multiplier=100, expiry=EXP, strike=K, right=r, underlying_id=99)
        g = v.greeks(c, spots[s], iv, at)
        px = v.price(c, spots[s], iv, at)
        S = spots[s]
        row = dict(leg=f"{s} {K}{r}", qty=q, iv=iv, price=round(px, 3), delta=round(g["delta"], 4), gamma=round(g["gamma"], 5),
                   pos_delta_sh=q * 100 * g["delta"], pos_delta_usd=q * 100 * g["delta"] * S,
                   pos_gamma_usd_1pct=q * 100 * g["gamma"] * S * S * 0.0001 * 0.5,  # $ P&L from a 1% move via gamma
                   pos_gamma_sh_per_usd=q * 100 * g["gamma"],
                   pos_vega_per_volpt=q * 100 * g["vega"] / 100, pos_theta_day=q * 100 * g["theta"], value=q * 100 * px)
        rows.append(row)
        agg = tot.setdefault(s, {})
        for key in ["pos_delta_sh", "pos_delta_usd", "pos_gamma_sh_per_usd", "pos_gamma_usd_1pct", "pos_vega_per_volpt", "pos_theta_day", "value"]:
            agg[key] = agg.get(key, 0) + row[key]
    return rows, tot

if __name__ == "__main__":
    out = {}
    for label, at in [("Oct20 15:45", datetime(2026, 10, 20, 15, 45, tzinfo=NY)), ("Day0 close Oct21", datetime(2026, 10, 21, 16, 0, tzinfo=NY)),
                      ("Oct28 close", datetime(2026, 10, 28, 16, 0, tzinfo=NY)), ("Nov4 close", datetime(2026, 11, 4, 16, 0, tzinfo=NY))]:
        rows, tot = greeks_at(at)
        print("==", label)
        if label.startswith("Oct20"):
            for r in rows:
                print({k: (round(x, 2) if isinstance(x, float) else x) for k, x in r.items()})
        for s, a in tot.items():
            print(s, {k: round(x, 1) for k, x in a.items()})
        out[label] = tot
    json.dump(out, open(OUTDIR / "greeks.json", "w"), indent=1)

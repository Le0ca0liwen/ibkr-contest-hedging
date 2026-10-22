"""Per-session market tables: real 1-min paths replayed as relative moves from today's spots,
option model prices each second and Greeks every 5 s with the app's ValuationEngine at chain IVs."""
import os
import sys
import time as _t

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *  # noqa
from hedge.valuation import ValuationEngine

BARS = Path(os.environ.get("COOLDOWN_BARS", str(DATA)))  # a refreshed data dir (scripts/refresh.sh OUT)
sys.path.insert(0, str(BARS))
from replay_loader import official_close, spot_path  # noqa: E402

date_str = sys.argv[1]
(LANE / "market").mkdir(parents=True, exist_ok=True)
m = build_manifest(history_file(LANE / "market" / "daily.csv"))
eng = ValuationEngine(m)
tab = option_table()
t0, paths = spot_path(date_str, start_spot=TODAY_SPOT)
b = pd.read_csv(BARS / "bars_1m.csv")
opens = {s: float(b[(b.date == date_str) & (b.symbol == s)].sort_values("timestamp_et").open.iloc[0]) for s in TODAY_SPOT}
oc = official_close(date_str)
close_spot = {s: (TODAY_SPOT[s] * oc[s] / opens[s]) if oc.get(s) == oc.get(s) else float(paths[s][-1]) for s in TODAY_SPOT}
base = datetime.combine(SIM_DAY, time(9, 30), NY)
N = 23401
ids = sorted(OPTIONS)
stock = {"SPCX": 10, "RKLB": 20, "SPY": 30}
mid = {}
for s in TODAY_SPOT:
    p = paths[s]
    spread = STOCK_SPREAD[s]
    bid = np.floor((p - spread / 2) * 100 + 1e-9) / 100
    mid[s] = bid + spread / 2
price = np.empty((N, len(ids)))
greeks = np.empty((N // 5 + 1, len(ids), 5))
w0 = _t.perf_counter()
for j, cid in enumerate(ids):
    c = m.contract_map[cid]
    iv = tab[cid]["iv"]
    sp = mid[c.symbol]
    for k in range(N):
        price[k, j] = eng.price(c, float(sp[k]), iv, base + timedelta(seconds=k))
    for g, k in enumerate(range(0, N, 5)):
        gr = eng.greeks(c, float(sp[k]), iv, base + timedelta(seconds=k))
        greeks[g, j] = [gr["delta"], gr["gamma"], gr["vega"], gr["theta"], gr["rho"]]
    print(date_str, cid, round(_t.perf_counter() - w0, 1), flush=True)
close_price = np.array([eng.price(m.contract_map[cid], close_spot[m.contract_map[cid].symbol], tab[cid]["iv"],
                                  base.replace(hour=16)) for cid in ids])
np.savez_compressed(
    LANE / "market" / f"{date_str}.npz", ids=np.array(ids), price=price, greeks=greeks,
    spot_SPCX=paths["SPCX"], spot_RKLB=paths["RKLB"], spot_SPY=paths["SPY"],
    mid_SPCX=mid["SPCX"], mid_RKLB=mid["RKLB"], mid_SPY=mid["SPY"],
    close_spot=np.array([close_spot["SPCX"], close_spot["RKLB"], close_spot["SPY"]]), close_price=close_price,
    opens=np.array([opens["SPCX"], opens["RKLB"], opens["SPY"]]),
    iv=np.array([tab[c]["iv"] for c in ids]), rel_spread=np.array([tab[c]["rel_spread"] for c in ids]))
print("done", date_str, round(_t.perf_counter() - w0, 1))

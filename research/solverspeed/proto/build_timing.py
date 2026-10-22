import json, statistics, sys, time, warnings
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[3])]
warnings.simplefilter("ignore")
import cases, load
cases.SPLIT.update({23: (7, 8, 8), 29: (9, 9, 9), 32: (10, 10, 10), 35: (11, 11, 11)})
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
val = load.load("hedge.valuation_C", Path.home() / "ss-protoC" / "hedge" / "valuation.py", "hedge.valuation_C")
rows = []
for n in (12, 14, 17, 20, 26, 32, 38):
    for clock in ("10:00", "14:00", "15:40"):
        m, quotes, snap, prev, nav, now = cases.build_case(n, clock)
        ids = screen_options(m, quotes, snap.quantities())
        res = {}
        for name in ("base", "C"):
            eng = ScenarioEngine(m)
            if name == "C": eng.valuation = val.ValuationEngine(m)
            ts, cpu = [], []
            for _ in range(5):
                c0 = time.process_time(); t = time.perf_counter(); eng.build(snap, quotes, now, ids); ts.append(time.perf_counter() - t); cpu.append(time.process_time() - c0)
            res[name] = (ts[0], statistics.median(ts[1:]), max(ts), statistics.median(cpu))
        rows.append(dict(n=n, clock=clock, base=res["base"], C=res["C"]))
        print(f"n={n:>2} {clock} base first {res['base'][0]:.3f} med {res['base'][1]:.3f} max {res['base'][2]:.3f} | C first {res['C'][0]:.3f} med {res['C'][1]:.3f} max {res['C'][2]:.3f} | speedup {res['base'][1]/res['C'][1]:.1f}x", flush=True)
json.dump(rows, open("solverspeed/out/build_timing.json", "w"))

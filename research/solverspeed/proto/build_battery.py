"""Scenario-build equivalence for prototype C (cached QuantLib sessions) vs baseline pricing.

    python build_battery.py --n 40 --workers 12 --out out/build_battery_C.jsonl [--tree ~/ss-protoC]
Compares changes / next_changes / overnight_changes / stress / rhos / identifier for 40 random builds.
"""
import argparse, json, os, sys, warnings
from multiprocessing import Pool
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[2])]


def spec(seed):
    import random
    r = random.Random(seed)
    return dict(seed=seed, n=r.choice([12, 17, 20, 26, 38]), clock=r.choice(["09:45", "10:00", "11:30", "13:00", "14:00", "15:00", "15:40"]),
                spot=r.uniform(0.9, 1.1), iv=r.uniform(0.8, 1.3), gap=r.uniform(-3000, 3000))


def work(arg):
    sp, tree = arg
    os.environ.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    warnings.simplefilter("ignore")
    import numpy as np, cases, load
    from time import perf_counter
    from hedge.scenarios import ScenarioEngine
    from hedge.universe import screen_options
    table = min(["10:00", "14:00", "15:40"], key=lambda c: abs(int(c[:2]) * 60 + int(c[3:]) - (int(sp["clock"][:2]) * 60 + int(sp["clock"][3:]))))
    m, quotes, snap, prev, nav, now = cases.build_case(sp["n"], table, gap=sp["gap"])
    now = cases.clock_at(sp["clock"])
    quotes = {i: q.model_copy(update={"observed_at": now, "greek_at": now, "spot": q.spot * sp["spot"], **({"iv": q.iv * sp["iv"]} if q.iv else {})}) for i, q in quotes.items()}
    snap = snap.model_copy(update={"at": now, "account_at": now})
    ids = screen_options(m, quotes, snap.quantities())
    val = load.load("hedge.valuation_C", Path(tree or Path.home() / "ss-protoC") / "hedge" / "valuation.py", "hedge.valuation_C")
    # same package-relative imports as the original module
    base = ScenarioEngine(m); proto = ScenarioEngine(m); proto.valuation = val.ValuationEngine(m)
    t = perf_counter(); a = base.build(snap, quotes, now, ids); tb = perf_counter() - t
    t = perf_counter(); b = proto.build(snap, quotes, now, ids); tp = perf_counter() - t
    t = perf_counter(); b2 = proto.build(snap, quotes, now, ids); tp2 = perf_counter() - t
    worst = {}
    for name in ("changes", "stress", "next_changes", "overnight_changes"):
        x, y = getattr(a, name), getattr(b, name)
        worst[name] = 0.0 if x is None else float(np.max(np.abs(x - y) / np.maximum(1e-12, np.maximum(np.abs(x), 1.0))))
    worst["rhos"] = max((abs(a.rhos[i] - b.rhos[i]) / max(1.0, abs(a.rhos[i])) for i in a.rhos), default=0.0)
    exact = all(np.array_equal(getattr(a, k), getattr(b, k)) for k in ("changes", "stress")) and a.identifier == b.identifier
    return dict(spec=sp, max_rel=worst, bit_identical=bool(exact), same_id=a.identifier == b.identifier, rebuild_same=bool(b2.identifier == b.identifier), t=(tb, tp, tp2))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=40); ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", required=True); ap.add_argument("--tree", default=None); ap.add_argument("--seed0", type=int, default=5000)
    a = ap.parse_args()
    with Pool(a.workers) as p, open(a.out, "w") as f:
        for r in p.imap_unordered(work, [(spec(a.seed0 + k), a.tree) for k in range(a.n)]):
            f.write(json.dumps(r) + "\n")
    rows = [json.loads(l) for l in open(a.out)]
    print("builds", len(rows), "bit-identical", sum(r["bit_identical"] for r in rows), "same identifier", sum(r["same_id"] for r in rows),
          "rebuild stable", sum(r["rebuild_same"] for r in rows), "worst rel diff", max(max(r["max_rel"].values()) for r in rows))

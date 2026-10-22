"""Randomized equivalence battery: baseline (CVXPY) vs prototype optimizer on the same inputs.

    python solverspeed/proto/battery.py --n 48 --workers 12 --out solverspeed/out/battery_A.jsonl [--proto-tree DIR]

Per instance (seeded): random candidate count (12/14/17/20), clock, spot/IV/spread perturbation, book
(mandated only, or mandated + random holdings in the candidates), NAV gap, stock_only or full. Records
(a) whether every HiGHS model handed to HiGHS is bit-identical (LP arrays) and (b) the solve outcomes.
"""
import argparse, json, os, sys, warnings
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[2])]


def instance(seed):
    import random
    r = random.Random(seed)
    return {"seed": seed, "n": r.choice([12, 14, 17, 20]), "clock": r.choice(["09:45", "10:00", "11:30", "13:00", "14:00", "15:00", "15:40"]),
            "spot": r.uniform(0.95, 1.05), "iv": r.uniform(0.85, 1.2), "spread": r.uniform(0.7, 1.8),
            "hedged": r.random() < 0.5, "gap": r.uniform(-4000, 4000), "stock_only": r.random() < 0.2}


def work(args):
    spec, tree = args
    os.environ.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    warnings.simplefilter("ignore")
    import random
    import numpy as np
    import cases, lpdiff
    from decimal import Decimal
    from hedge.models import Position
    from hedge.scenarios import ScenarioEngine
    from hedge.universe import screen_options
    r = random.Random(spec["seed"] * 7 + 1)
    # cases.quote_table only knows 10:00/14:00/15:40 clocks; reuse nearest table, price scenarios at the real clock
    table_clock = min(["10:00", "14:00", "15:40"], key=lambda c: abs(int(c[:2]) * 60 + int(c[3:]) - (int(spec["clock"][:2]) * 60 + int(spec["clock"][3:]))))
    m, quotes, snap, prev, nav, now = cases.build_case(spec["n"], table_clock, gap=spec["gap"])
    now = cases.clock_at(spec["clock"])
    quotes2 = {}
    for i, q in quotes.items():
        spread = (q.ask - q.bid) * spec["spread"]
        upd = {"observed_at": now, "greek_at": now, "spot": q.spot * spec["spot"]}
        if q.iv:
            upd["iv"] = q.iv * spec["iv"]
        mid = q.mid * (spec["spot"] if m.contract_map[i].kind == "STK" else 1)
        upd["bid"], upd["ask"] = round(max(0.01, mid - spread / 2), 2), round(max(0.02, mid + spread / 2), 2)
        quotes2[i] = q.model_copy(update=upd)
    positions = list(snap.positions)
    if spec["hedged"]:
        held = {p.con_id for p in positions}
        for i in sorted(quotes2):
            c = m.contract_map[i]
            if i in m.allowlist and i not in held and r.random() < 0.3:
                positions.append(Position(con_id=i, quantity=r.randint(-8, 8) * (50 if c.kind == "STK" else 1) or 1))
    snap = snap.model_copy(update={"at": now, "account_at": now, "positions": tuple(positions)})
    ids = screen_options(m, quotes2, snap.quantities())
    scen = ScenarioEngine(m).build(snap, quotes2, now, ids)
    P = lpdiff.load.proto_optimizer(tree)
    from hedge.optimizer import HedgeOptimizer
    lpdiff.CAPTURED.clear(); rb = HedgeOptimizer(m).solve(snap, quotes2, scen, prev, nav, spec["stock_only"]); base = list(lpdiff.CAPTURED)
    lpdiff.CAPTURED.clear(); rp = P.HedgeOptimizer(m).solve(snap, quotes2, scen, prev, nav, spec["stock_only"]); proto = list(lpdiff.CAPTURED)
    cmp = [lpdiff.compare(a, b) for a, b in zip(base, proto)]
    exact = len(base) == len(proto) and all(c.get("A_exact") and all(v is True for k, v in c.items() if k.endswith("_") or k == "integrality") for c in cmp)
    def stages(res):
        return [(s["stage"], s["status"], s.get("incumbent"), s.get("dual_bound")) for s in res.solver_metrics["stages"]]
    sb, sp = stages(rb), stages(rp)
    out = {"spec": spec, "lp_identical": exact, "lp_stages": (len(base), len(proto)),
           "status": (rb.status, rp.status), "same_quantities": rb.quantities == rp.quantities,
           "score": (rb.expected_score, rp.expected_score), "stages_base": sb, "stages_proto": sp,
           "total_ms": (rb.solver_metrics["total_ms"], rp.solver_metrics["total_ms"]),
           "primary_base": rb.solver_metrics.get("primary_incumbent"), "primary_proto": rp.solver_metrics.get("primary_incumbent"),
           "bound_base": rb.solver_metrics.get("primary_lower_bound"), "bound_proto": rp.solver_metrics.get("primary_lower_bound"),
           "ranking": (rb.solver_metrics.get("ranking_complete"), rp.solver_metrics.get("ranking_complete")),
           "candidates": rb.solver_metrics.get("candidate_count"),
           "qb": {str(k): v for k, v in sorted(rb.quantities.items())}, "qp": {str(k): v for k, v in sorted(rp.quantities.items())}}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=48); ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--seed0", type=int, default=1000); ap.add_argument("--out", required=True); ap.add_argument("--proto-tree", default=None)
    a = ap.parse_args()
    specs = [(instance(a.seed0 + k), a.proto_tree) for k in range(a.n)]
    with Pool(a.workers) as pool, open(a.out, "w") as f:
        for res in pool.imap_unordered(work, specs):
            f.write(json.dumps(res) + "\n"); f.flush()
    print("done", a.n)

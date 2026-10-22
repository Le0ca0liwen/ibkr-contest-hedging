"""Diagnosis: (1) cost of following minute-to-minute targets, (2) modeled vs realized benefit of
eligible plans (proposed change executed at decision-time quotes, crossing half-spread + fee, held to
the official close), per variant. python diag_realize.py RUN [RUN...]"""
import json, os, sys, statistics as st
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ReplayMarket, build_manifest, OPEN, LANE  # noqa: E402

m = build_manifest({}, {})
H0 = {int(k): v for k, v in json.loads((LANE / "h0.json").read_text()).items()}
markets = {}

def fee(c, n):
    rate = m.policy.commission_option if c.kind == "OPT" else m.policy.commission_share
    return max(m.policy.minimum_commission, rate * abs(n)) if n else 0.0

res = {}
for run in sys.argv[1:]:
    rep = json.load(open(f"{run}/report.json"))
    day = rep["day"]
    mk = markets.setdefault(day, ReplayMarket(m, day))
    marks = {int(k): v for k, v in rep["close_marks"].items()}
    fills = sorted(rep["fills"], key=lambda f: f["sec"])
    G = rep["gap"]
    def held_at(at):
        h = dict(H0)
        for f in fills:
            if f["at"] >= at: break
            h[f["con_id"]] = h.get(f["con_id"], 0) + f["qty"]
        return h
    full = sorted([d for d in rep["decisions"] if d.get("optimization_scope") == "FULL" and d.get("status") == "OPTIMAL"], key=lambda d: d["at"])
    # (1) cost to follow the target path (options only), crossing half-spread + fee per change
    follow = 0.0; prev = None
    for d in full:
        q = {int(k): v for k, v in d["quantities"].items()}
        if prev is not None:
            t = datetime.fromisoformat(d["at"]); qs = mk.quotes(t)
            for i in set(q) | set(prev):
                c = m.contract_map[i]
                if c.kind != "OPT" or i in m.mandatory: continue
                n = q.get(i, 0) - prev.get(i, 0)
                if n: follow += abs(n) * (qs[i].ask - qs[i].bid) / 2 * 100 + fee(c, n)
        prev = q
    # (2) eligible plans: modeled improvement vs realized at close
    rows = []
    for d in rep["decisions"]:
        if not d.get("trade_eligible"): continue
        t = datetime.fromisoformat(d["at"]); qs = mk.quotes(t)
        held = held_at(d["at"]); q = {int(k): v for k, v in d["quantities"].items()}
        pnl = 0.0; cross = 0.0; nopt = 0
        for i in set(q) | set(held):
            n = q.get(i, 0) - held.get(i, 0)
            if not n: continue
            c = m.contract_map[i]; mult = c.multiplier
            px = qs[i].ask if n > 0 else qs[i].bid
            cross += abs(n) * (qs[i].ask - qs[i].bid) / 2 * mult + fee(c, n)
            pnl += n * mult * (marks[i] - px) - fee(c, n)
            if c.kind == "OPT": nopt += abs(n)
        rows.append({"at": d["at"], "modeled": d["improvement"], "realized": abs(G) - abs(G + pnl), "cross": cross, "nopt": nopt,
                     "scope": d.get("optimization_scope")})
    res[run] = {"follow_cost_opt": follow, "full_solves": len(full), "eligible": len(rows),
                "modeled_mean": st.mean(r["modeled"] for r in rows) if rows else None,
                "realized_mean": st.mean(r["realized"] for r in rows) if rows else None,
                "realized_median": st.median(r["realized"] for r in rows) if rows else None,
                "frac_realized_pos": sum(r["realized"] > 0 for r in rows) / len(rows) if rows else None,
                "cross_mean": st.mean(r["cross"] for r in rows) if rows else None,
                "nopt_mean": st.mean(r["nopt"] for r in rows) if rows else None,
                "small_modeled_mean": st.mean(r["modeled"] for r in rows if r["modeled"] < 150) if any(r["modeled"] < 150 for r in rows) else None,
                "small_realized_mean": st.mean(r["realized"] for r in rows if r["modeled"] < 150) if any(r["modeled"] < 150 for r in rows) else None,
                "n_small": sum(r["modeled"] < 150 for r in rows)}
    print(run, json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in res[run].items()}), flush=True)
(LANE / "diag").mkdir(parents=True, exist_ok=True)
json.dump(res, open(LANE / "diag" / ("realize_" + "_".join(os.path.basename(a.rstrip("/")) for a in sys.argv[1:2]) + ".json"), "w"))

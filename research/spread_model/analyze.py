"""Summaries for spread_model runs: per-run metrics, plan-level modeled vs realized benefit, reversals.

python analyze.py RUN_DIR [RUN_DIR ...]        -> one JSON line per run (also returned by summarize())
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

LANE = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "spread_model"
OPTION_IDS = set(range(1001, 1018))


def load(run):
    run = Path(run)
    rows = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines() if line.strip()]
    report = json.loads((run / "report.json").read_text())
    return rows, report


def summarize(run):
    rows, rep = load(run)
    marks = {int(k): v for k, v in rep["close_marks"].items()}
    fills = rep["fills"]
    submits = {r["order_id"]: r for r in rows if r["ev"] == "submit"}
    mult = lambda f: 100 if f["kind"] == "OPT" else 1  # noqa: E731
    target, nav = rep["target"], rep["nav_close"]
    # per-contract volume and reversals
    gross, net = defaultdict(float), defaultdict(float)
    for f in fills:
        gross[f["con_id"]] += abs(f["qty"])
        net[f["con_id"]] += f["qty"]
    opt_gross = sum(v for i, v in gross.items() if i in OPTION_IDS)
    opt_net = sum(abs(net[i]) for i in gross if i in OPTION_IDS)
    stk_gross = sum(v for i, v in gross.items() if i not in OPTION_IDS)
    stk_net = sum(abs(net[i]) for i in gross if i not in OPTION_IDS)
    # spread paid split
    opt_spread = sum(f["spread_paid"] for f in fills if f["kind"] == "OPT")
    stk_spread = sum(f["spread_paid"] for f in fills if f["kind"] == "STK")
    opt_fees = sum(f["fee"] for f in fills if f["kind"] == "OPT")
    stk_fees = sum(f["fee"] for f in fills if f["kind"] == "STK")
    opt_rel = [
        (f["price"] - f["mid"]) * (1 if f["qty"] > 0 else -1) / (f["ask"] - f["bid"])
        for f in fills
        if f["kind"] == "OPT" and f["ask"] > f["bid"]
    ]
    # plans: fills grouped by the decision that submitted them
    plans = defaultdict(list)
    for f in fills:
        decision = submits.get(f["order_id"], {}).get("decision_id") or f.get("decision_id")
        plans[decision].append(f)
    decisions = {d.get("id"): d for d in rep.get("decisions", []) if d.get("id")}
    by_id = dict(zip(rep.get("decision_ids", []), rep.get("decisions", []), strict=False))
    plan_rows = []
    for decision, fs in plans.items():
        pnl = sum(f["qty"] * mult(f) * (marks[f["con_id"]] - f["price"]) - f["fee"] for f in fs)
        realized = abs(nav - pnl - target) - abs(nav - target)
        d = by_id.get(decision) or decisions.get(decision) or {}
        plan_rows.append(
            {
                "decision": decision,
                "at": d.get("at"),
                "scope": d.get("optimization_scope"),
                "modeled_improvement": d.get("improvement"),
                "fills": len(fs),
                "options": sum(abs(f["qty"]) for f in fs if f["kind"] == "OPT"),
                "pnl_to_close": pnl,
                "realized_benefit": realized,
                "spread_paid": sum(f["spread_paid"] for f in fs),
            }
        )
    eligible = [d for d in rep.get("decisions", []) if d.get("trade_eligible")]
    full = [d for d in rep.get("decisions", []) if d.get("optimization_scope") == "FULL"]
    # Plan directions: each eligible decision's proposed change (target - holdings at that time).
    h0 = {int(k): v for k, v in json.loads((LANE / "h0.json").read_text()).items()}
    fills_sorted = sorted(fills, key=lambda f: f["sec"])
    def holdings_at(at):
        held = dict(h0)
        for f in fills_sorted:
            if f["at"] >= at:
                break
            held[f["con_id"]] = held.get(f["con_id"], 0) + f["qty"]
        return held
    plan_dirs = defaultdict(list)  # con_id -> [(at, signed change)]
    eligible_rows = []
    for d in sorted(eligible, key=lambda x: x["at"]):
        held = holdings_at(d["at"])
        q = {int(k): v for k, v in (d.get("quantities") or {}).items()}
        change = {i: q.get(i, 0) - held.get(i, 0) for i in set(q) | set(held) if q.get(i, 0) != held.get(i, 0)}
        opt_change = {i: v for i, v in change.items() if i in OPTION_IDS}
        for i, v in opt_change.items():
            plan_dirs[i].append((d["at"], v))
        eligible_rows.append({"at": d["at"], "scope": d.get("optimization_scope"), "improvement": d.get("improvement"),
                              "gross": d.get("gross_improvement"), "reserve": d.get("reversal_reserve"),
                              "threshold": d.get("threshold"), "opt_change": opt_change,
                              "stk_change": {i: v for i, v in change.items() if i not in OPTION_IDS}})
    flips = sum(
        1 for seq in plan_dirs.values() for (a, x), (b, y) in zip(seq, seq[1:]) if x * y < 0
    )
    ticks = [r for r in rows if r["ev"] == "tick"]
    pending_minutes = sum(1 for r in ticks if r.get("pending") or r.get("orders"))
    paused = any(r.get("mode") == "PAUSED" for r in ticks)
    scored = [d for d in rep.get("decisions", []) if d.get("current_score") is not None and d.get("status") == "OPTIMAL"]
    closing = [
        (d.get("components") or {}).get("closing") for d in scored if (d.get("components") or {}).get("closing") is not None
    ]
    navs = [r["nav"] for r in ticks]
    steps = [b - a for a, b in zip(navs, navs[1:], strict=False)]
    nav_rv = (sum(x * x for x in steps)) ** 0.5 if steps else None
    # Path-luck-free expected miss: E|g_open - costs + RV * Z|, Z ~ N(0,1), RV = realized intraday NAV
    # variation of this variant's book, g_open = NAV at the open - target (identical across variants).
    import math

    g_open = (ticks[0]["nav"] - target) if ticks else 0.0
    m_ = g_open - (rep["spread_paid"] + rep["fees"])
    if nav_rv:
        z = m_ / nav_rv
        expected_miss = nav_rv * math.sqrt(2 / math.pi) * math.exp(-z * z / 2) + m_ * math.erf(z / math.sqrt(2))
    else:
        expected_miss = abs(m_)
    return {
        "g_open": g_open,
        "expected_miss_rv": expected_miss,
        "nav_rv_1m": nav_rv,
        "mean_modeled_score_held": st.mean(d["current_score"] for d in scored) if scored else None,
        "pending_minutes": pending_minutes,
        "hard_stopped": paused,
        "name": rep["name"],
        "day": rep["day"],
        "miss": rep["miss"],
        "gap": rep["gap"],
        "miss_hold": rep["miss_hold"],
        "spread_paid": rep["spread_paid"],
        "fees": rep["fees"],
        "cost": rep["spread_paid"] + rep["fees"],
        "opt_spread": opt_spread,
        "stk_spread": stk_spread,
        "opt_fees": opt_fees,
        "stk_fees": stk_fees,
        "opt_contracts": opt_gross,
        "opt_net": opt_net,
        "opt_reversed": (opt_gross - opt_net) / 2,
        "stk_shares": stk_gross,
        "stk_reversed": (stk_gross - stk_net) / 2,
        "fills_opt": sum(1 for f in fills if f["kind"] == "OPT"),
        "fills_stk": sum(1 for f in fills if f["kind"] == "STK"),
        "opt_fill_cost_frac": sum(opt_rel) / len(opt_rel) if opt_rel else None,
        "decisions": len(rep.get("decisions", [])),
        "eligible": len(eligible),
        "full_solves": len(full),
        "plan_flips": flips,
        "eligible_rows": eligible_rows,
        "plans": plan_rows,
        "final_positions": rep["final_positions"],
        "solves": rep["solves"],
        "wall_s": rep["wall_s"],
        "mode": rep["mode"],
    }


if __name__ == "__main__":
    for run in sys.argv[1:]:
        s = summarize(run)
        s.pop("plans")
        s.pop("eligible_rows")
        print(json.dumps(s, default=str))

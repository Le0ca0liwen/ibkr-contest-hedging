"""Per-run threshold metrics: python analyze_thr.py RUN_DIR [RUN_DIR ...] -> JSON lines on stdout.

Hedger vs burner orders are told apart by the submit log's urgency (BURN). A "plan" is one
hedge decision whose first slice was transmitted (it starts the trade cooldowns).
Realized plan effect at the close (marginal counterfactual, later actions held fixed):
    P_i = sum over the plan's fills of qty * mult * (closing mid - fill price) - fee
    G   = closing NAV - target
    realized_i = |G - P_i| - |G|   (> 0: the plan made the 16:00 miss smaller)
"""

import json
import sys
from bisect import bisect_right
from datetime import datetime
from pathlib import Path


def load(run):
    rows = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]
    report = json.loads((run / "report.json").read_text())
    return rows, report


def t(value):
    return datetime.fromisoformat(value)


def analyze(run):
    rows, report = load(run)
    market = next(r for r in rows if r["kind"] == "market")
    base = next(r for r in rows if r["kind"] == "baseline")
    target = float(base["target"])
    score_threshold = float(market["score_threshold"])
    decisions = sorted(report["decisions"], key=lambda d: d["at"])
    by_id = {d["id"]: d for d in decisions}
    solves = [r for r in rows if r["kind"] == "solve"]
    solve_at = {}
    for s in solves:
        solve_at.setdefault(t(s["at"]), s)

    # --- decisions / eligibility
    optimal = [d for d in decisions if d["status"] == "OPTIMAL"]
    eligible = [d for d in decisions if d["trade_eligible"]]
    corrective_eligible = [d for d in eligible if d.get("corrective")]
    gate_ok = [d for d in optimal if d.get("risk_passed")]
    unc_term = [1.645 * (d.get("uncertainty") or 0) for d in gate_ok]
    binding = sum(u < score_threshold for u in unc_term)  # score_threshold (not uncertainty) sets the bar
    band = [  # eligible here, but not at $100 (same uncertainty term)
        d for d in eligible
        if not d.get("corrective") and d["improvement"] <= max(100.0, 1.645 * (d.get("uncertainty") or 0))
    ]
    def flips(lower):  # eligible at `lower`, not at 100, same decision (gate passed, OPTIMAL, not corrective)
        out = []
        for d in gate_ok:
            if d.get("corrective"):
                continue
            u = 1.645 * (d.get("uncertainty") or 0)
            if max(lower, u) < d["improvement"] <= max(100.0, u):
                out.append(d)
        return out

    flip50, flip30 = flips(50.0), flips(30.0)
    in_band = [d for d in gate_ok if not d.get("corrective") and 30 < d["improvement"] <= 100]
    near_miss = [  # gate-passed OPTIMAL, not corrective, improvement in (30, 100] and uncertainty term < 100
        d for d in gate_ok
        if not d.get("corrective") and 30 < d["improvement"] <= 100 and 1.645 * (d.get("uncertainty") or 0) < 100
    ]

    # --- orders / fills
    submits = [r for r in rows if r["kind"] == "submit"]
    urgency = {r["order_id"]: r["urgency"] for r in submits}
    hedge_submits = [r for r in submits if r["urgency"] != "BURN"]
    burn_submits = [r for r in submits if r["urgency"] == "BURN"]
    fills = [r for r in rows if r["kind"] == "fill"]
    intents = {i["id"]: i for i in report["intents"]}

    def is_burn(f):
        return urgency.get(f["order_id"]) == "BURN"

    hedge_fills = [f for f in fills if not is_burn(f)]
    burn_fills = [f for f in fills if is_burn(f)]

    def modeled_cost(f):  # optimizer.execution_cost for this fill: |q| * spread * (0.5 + 0.1) * mult + fee
        return abs(f["qty"]) * (f["ask"] - f["bid"]) * 0.6 * f["mult"] + f["fee"]

    def costs(fs):
        total = sum(f["cost"] for f in fs)
        fee = sum(f["fee"] for f in fs)
        return {
            "modeled_for_fills": round(sum(modeled_cost(f) for f in fs), 2),
            "total": round(total, 2),
            "spread": round(total - fee, 2),
            "fees": round(fee, 2),
            "option_contracts": sum(abs(f["qty"]) for f in fs if f["sec_type"] == "OPT"),
            "shares": sum(abs(f["qty"]) for f in fs if f["sec_type"] == "STK"),
            "fills": len(fs),
        }

    # --- plans and cooldowns
    starts = []
    for r in rows:  # early runs logged kind=STK/OPT (the kwarg overwrote the row kind)
        if r["kind"] == "cooldown_start":
            starts.append({**r, "kind": r["cd_kind"]})
        elif r["kind"] in ("STK", "OPT") and "decision_id" in r:
            starts.append(r)
    blocks = [r for r in rows if r["kind"] == "cooldown_block"]
    plan_ids = []
    for r in starts:
        if r["decision_id"] not in plan_ids:
            plan_ids.append(r["decision_id"])
    opt_starts = [r for r in starts if r["kind"] == "OPT"]
    stk_starts = [r for r in starts if r["kind"] == "STK"]

    def plan_improvement(decision_id):
        d = by_id.get(decision_id)
        return None if d is None else d["improvement"]

    block_rows = []
    for b in blocks:
        at = t(b["at"])
        blockers = []
        for kind in ("OPT", "STK"):
            if b["left"].get(kind, 0) > 0 and kind in b["kinds"]:
                prior = [s for s in starts if s["kind"] == kind and t(s["at"]) <= at]
                if prior:
                    starter = prior[-1]
                    blockers.append(
                        {
                            "kind": kind,
                            "starter": starter["decision_id"],
                            "starter_improvement": plan_improvement(starter["decision_id"]),
                            "starter_corrective": (by_id.get(starter["decision_id"]) or {}).get("corrective"),
                            "left_s": round(b["left"][kind], 1),
                        }
                    )
        blocked = by_id.get(b["decision_id"]) or {}
        block_rows.append(
            {
                "at": b["at"][11:19],
                "blocked": b["decision_id"],
                "blocked_improvement": blocked.get("improvement"),
                "blocked_scope": blocked.get("optimization_scope"),
                "blockers": blockers,
            }
        )
    opt_blocks = [x for x in block_rows if any(bl["kind"] == "OPT" for bl in x["blockers"])]
    small_opt_blocks = [
        x for x in opt_blocks
        if any(bl["kind"] == "OPT" and bl["starter_improvement"] is not None and bl["starter_improvement"] < 100
               and not bl["starter_corrective"] for bl in x["blockers"])
    ]

    # --- close
    ledger_day = report["ledger"]["days"][0]
    close_obs = next((r for r in rows if r["kind"] == "closing_obs"), None)
    close_nav = float(ledger_day["nav"]) if ledger_day.get("nav") else None
    gap = close_nav - target if close_nav is not None else None
    mids = {int(k): v for k, v in (close_obs or {}).get("mids", {}).items()}

    ticks = [r for r in rows if r["kind"] == "tick"]
    tick_times = [t(r["at"]) for r in ticks]

    def gap_at(hhmmss):
        when = datetime.fromisoformat(ticks[0]["at"][:11] + hhmmss + ticks[0]["at"][19:])
        i = bisect_right(tick_times, when) - 1
        return None if i < 0 else round(ticks[i]["nav"] - target, 2)

    # --- per-plan modeled vs realized
    plans = []
    fills_by_decision = {}
    for f in hedge_fills:
        intent = intents.get(f["order_ref"].split(":", 1)[1])
        if intent is None:
            continue
        fills_by_decision.setdefault(intent["decision_id"], []).append(f)
    for decision_id, fs in fills_by_decision.items():
        d = by_id.get(decision_id, {})
        s = solve_at.get(t(d["at"])) if d else None
        pnl = sum(f["qty"] * f["mult"] * (mids.get(f["con_id"], f["mid"]) - f["price"]) - f["fee"] for f in fs)
        realized = abs(gap - pnl) - abs(gap) if gap is not None else None
        first = min(t(f["at"]) for f in fs)
        plans.append(
            {
                "decision": decision_id,
                "at": d.get("at", "")[11:19],
                "first_fill": first.strftime("%H:%M:%S"),
                "scope": d.get("optimization_scope"),
                "corrective": d.get("corrective"),
                "modeled": d.get("improvement"),
                "modeled_closing": (s or {}).get("closing_impr"),
                "modeled_overnight": (s or {}).get("overnight_impr"),
                "threshold": d.get("threshold"),
                "uncertainty_term": 1.645 * (d.get("uncertainty") or 0),
                "modeled_cost": d.get("estimated_cost"),
                "realized_cost": round(sum(f["cost"] for f in fs), 2),
                "modeled_cost_fills": round(sum(modeled_cost(f) for f in fs), 2),
                "option_contracts": sum(abs(f["qty"]) for f in fs if f["sec_type"] == "OPT"),
                "shares": sum(abs(f["qty"]) for f in fs if f["sec_type"] == "STK"),
                "pnl_to_close": round(pnl, 2),
                "realized": round(realized, 2) if realized is not None else None,
            }
        )
    plans.sort(key=lambda p: p["first_fill"])

    timeouts = [s for s in solves if any(st[1] not in ("optimal",) and st[0] == "primary" for st in s.get("stages", []))]
    stock_rank_incomplete = [
        s for s in solves if s.get("stock_only") and s["status"] == "OPTIMAL" and s.get("accepted_stage") != "turnover"
    ]
    end = next((r for r in rows if r["kind"] == "end"), {})
    burn_results = report.get("burn_results", [])
    return {
        "run": run.name,
        "path": market["path"],
        "seed": market["seed"],
        "vol_scale": market["vol_scale"],
        "threshold": score_threshold,
        "decisions": len(decisions),
        "decisions_full": sum(d["optimization_scope"] == "FULL" for d in decisions),
        "decisions_optimal": len(optimal),
        "gate_passed_optimal": len(gate_ok),
        "score_threshold_binding": binding,
        "uncertainty_term_median": round(sorted(unc_term)[len(unc_term) // 2], 1) if unc_term else None,
        "eligible": len(eligible),
        "eligible_corrective": len(corrective_eligible),
        "eligible_below_100_band": len(band),
        "near_miss_30_100": len(near_miss),
        "flip_at_50": len(flip50),
        "flip_at_30": len(flip30),
        "impr_in_30_100": len(in_band),
        "impr_in_30_100_blocked_by_uncertainty": sum(1.645 * (d.get("uncertainty") or 0) >= d["improvement"] for d in in_band),
        "flip30_detail": [(d["at"][11:19], d["optimization_scope"], round(d["improvement"], 1),
                           round(1.645 * (d.get("uncertainty") or 0), 1)) for d in flip30],
        "plans": len(plan_ids),
        "plans_small_lt100": sum(
            1 for p in plan_ids
            if plan_improvement(p) is not None and plan_improvement(p) < 100 and not by_id[p].get("corrective")
        ),
        "plans_corrective": sum(1 for p in plan_ids if (by_id.get(p) or {}).get("corrective")),
        "hedge_orders": len(hedge_submits),
        "burn_orders": len(burn_submits),
        "hedge": costs(hedge_fills),
        "burn": costs(burn_fills),
        "burn_cycles": len(burn_results),
        "burn_realized_loss": round(sum(r["realized_loss"] for r in burn_results), 2),
        "opt_cooldowns": len(opt_starts),
        "stk_cooldowns": len(stk_starts),
        "cooldown_blocks": len(block_rows),
        "opt_cooldown_blocks": len(opt_blocks),
        "opt_blocks_by_small_plan": len(small_opt_blocks),
        "opt_blocked_decisions": sorted({x["blocked"] for x in opt_blocks}).__len__(),
        "opt_blocked_by_small_decisions": sorted({x["blocked"] for x in small_opt_blocks}).__len__(),
        "close_nav": close_nav,
        "target": round(target, 2),
        "close_gap": round(gap, 2) if gap is not None else None,
        "close_miss": round(abs(gap), 2) if gap is not None else None,
        "gap_1200": gap_at("12:00:00"),
        "gap_1539": gap_at("15:39:59"),
        "gap_1555": gap_at("15:55:00"),
        "final_positions": {k: v for k, v in report["final_positions"].items()},
        "plans_detail": plans,
        "blocks_detail": block_rows,
        "primary_not_optimal_solves": len(timeouts),
        "stock_ranking_incomplete": len(stock_rank_incomplete),
        "solves": len(solves),
        "statuses": {k: sum(d["status"] == k for d in decisions) for k in sorted({d["status"] for d in decisions})},
        "eligible_dropped_by_slicer": sum(
            1 for r in rows if r["kind"] == "plan_dropped" and r.get("health", "").startswith("No economical execution slice")
        ),
        "unfilled_plans": [pid for pid in plan_ids if pid not in fills_by_decision],
        "unfilled_plan_improvements": [round(plan_improvement(pid) or 0, 1) for pid in plan_ids if pid not in fills_by_decision],
        "wall_s": end.get("wall_s"),
        "end_at": end.get("at"),
        "task_exceptions": [r for r in rows if r["kind"] == "task_exception"],
        "mode": report.get("mode"),
    }


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(json.dumps(analyze(Path(arg)), default=str))

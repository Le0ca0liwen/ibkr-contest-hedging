"""Aggregate spread_model runs by variant: python compare.py PREFIX [PREFIX ...] (e.g. A_ B_)."""

from __future__ import annotations

import json
import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

from analyze import summarize

RUNS = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "spread_model" / "runs"


def variant_of(name):
    parts = name.split("_")
    return "_".join(parts[:-1])


def collect(prefixes):
    rows = []
    for d in sorted(RUNS.iterdir()):
        if any(d.name.startswith(p) for p in prefixes) and (d / "report.json").exists():
            if "skipped" in json.loads((d / "report.json").read_text()):
                continue
            rows.append(summarize(d))
    return rows


def table(rows):
    by = defaultdict(list)
    for r in rows:
        by[variant_of(r["name"])].append(r)
    days = sorted({r["day"] for r in rows})
    out = []
    for v, rs in sorted(by.items()):
        n = len(rs)
        plans = [p for r in rs for p in r["plans"]]
        realized = [p["realized_benefit"] for p in plans]
        modeled = [p["modeled_improvement"] for p in plans if p["modeled_improvement"] is not None]
        out.append(
            {
                "variant": v,
                "sessions": n,
                "days": sorted(r["day"] for r in rs),
                "mean_miss": st.mean(r["miss"] for r in rs),
                "median_miss": st.median(r["miss"] for r in rs),
                "mean_miss_hold": st.mean(r["miss_hold"] for r in rs),
                "spread_per_day": st.mean(r["spread_paid"] for r in rs),
                "fees_per_day": st.mean(r["fees"] for r in rs),
                "opt_spread_per_day": st.mean(r["opt_spread"] for r in rs),
                "opt_contracts_per_day": st.mean(r["opt_contracts"] for r in rs),
                "opt_reversed_per_day": st.mean(r["opt_reversed"] for r in rs),
                "stk_shares_per_day": st.mean(r["stk_shares"] for r in rs),
                "stk_reversed_per_day": st.mean(r["stk_reversed"] for r in rs),
                "fills_per_day": st.mean(r["fills_opt"] + r["fills_stk"] for r in rs),
                "eligible_plans_per_day": st.mean(r["eligible"] for r in rs),
                "plan_flips_per_day": st.mean(r["plan_flips"] for r in rs),
                "executed_plans_per_day": len(plans) / n,
                "plan_realized_mean": st.mean(realized) if realized else None,
                "plan_modeled_mean": st.mean(modeled) if modeled else None,
                "opt_fill_cost_frac": st.mean([r["opt_fill_cost_frac"] for r in rs if r["opt_fill_cost_frac"] is not None])
                if any(r["opt_fill_cost_frac"] is not None for r in rs) else None,
                "pending_minutes_per_day": st.mean(r["pending_minutes"] for r in rs),
                "mean_modeled_score_held": st.mean(r["mean_modeled_score_held"] for r in rs if r["mean_modeled_score_held"] is not None),
                "hard_stops": sum(r["hard_stopped"] for r in rs),
                "per_day": {r["day"]: round(r["miss"], 1) for r in rs},
            }
        )
    return out, days


def paired(rows, a, b):
    """Per-session miss difference b - a on sessions both ran."""
    A = {r["day"]: r for r in rows if variant_of(r["name"]) == a}
    B = {r["day"]: r for r in rows if variant_of(r["name"]) == b}
    common = sorted(set(A) & set(B))
    diffs = [B[d]["miss"] - A[d]["miss"] for d in common]
    costs = [(B[d]["spread_paid"] + B[d]["fees"]) - (A[d]["spread_paid"] + A[d]["fees"]) for d in common]
    return {"pair": f"{b} - {a}", "days": common, "miss_diff": [round(x, 1) for x in diffs],
            "mean_miss_diff": st.mean(diffs) if diffs else None, "mean_cost_diff": st.mean(costs) if costs else None}


if __name__ == "__main__":
    rows = collect(sys.argv[1:] or ["A_"])
    out, days = table(rows)
    for o in out:
        print(json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()}))

"""Final tables for the spread_model lane: per-variant metrics, paired differences vs B900, diagnostics."""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict

from analyze import summarize
from compare import RUNS, variant_of

LABEL = {
    "A_B900": "B900 baseline (option_seconds 900, standalone slice gate)",
    "A_C300": "C300 short cooldown (option_seconds 300)",
    "A_C60": "C60 short cooldown (option_seconds 60)",
    "A_P3": "P3 final prototype (60 s solves, reserve 1.0, plan slices: path bands + executable-part check, cap hysteresis 97%)",
    "A_C3q": "C3q ablation (= P3 without the reversal reserve)",
    "T2_P3": "P3 with touch-only fills",
    "A_P60": "P60 v2 prototype (before the ride-along/path-band fix; 98% gross headroom = path band)",
    "A_P60r1": "P60r1 ablation (reserve 1.0 only, standalone slice gate)",
    "A_C60q": "C60q ablation (plan slices + hysteresis, NO reserve)",
    "A_C60p": "C60p early ablation (plan slices, no reserve, no path headroom/hysteresis)",
    "A_P60r1p": "P60r1p early ablation (reserve + plan slices, no path headroom/hysteresis)",
    "T2_B900": "B900 with touch-only (IBKR-paper-like) fills",
    "T2_P60": "P60 with touch-only fills",
}


def main():
    rows = []
    for d in sorted(RUNS.iterdir()):
        rep = d / "report.json"
        if d.name.startswith(("A_", "T2_")) and rep.exists() and "skipped" not in json.loads(rep.read_text()):
            rows.append(summarize(d))
    by = defaultdict(dict)
    for r in rows:
        by[variant_of(r["name"])][r["day"]] = r
    base = by.get("A_B900", {})
    out = []
    for v, days in sorted(by.items()):
        rs = list(days.values())
        common = sorted(set(days) & set(base))
        paired_miss = [days[d]["miss"] - base[d]["miss"] for d in common] if v != "A_B900" else []
        paired_cost = [days[d]["cost"] - base[d]["cost"] for d in common] if v != "A_B900" else []
        paired_rv = [days[d]["nav_rv_1m"] - base[d]["nav_rv_1m"] for d in common] if v != "A_B900" else []
        paired_em = [days[d]["expected_miss_rv"] - base[d]["expected_miss_rv"] for d in common] if v != "A_B900" else []
        paired_model = (
            [days[d]["mean_modeled_score_held"] - base[d]["mean_modeled_score_held"] for d in common]
            if v != "A_B900"
            else []
        )
        out.append(
            {
                "variant": v,
                "label": LABEL.get(v, v),
                "days": sorted(days),
                "mean_miss": st.mean(r["miss"] for r in rs),
                "median_miss": st.median(r["miss"] for r in rs),
                "miss_hold_H0": st.mean(r["miss_hold"] for r in rs),
                "per_day_miss": {d: round(days[d]["miss"], 0) for d in sorted(days)},
                "spread_per_day": st.mean(r["spread_paid"] for r in rs),
                "fees_per_day": st.mean(r["fees"] for r in rs),
                "fills_per_day": st.mean(r["fills_opt"] + r["fills_stk"] for r in rs),
                "opt_fills_per_day": st.mean(r["fills_opt"] for r in rs),
                "stk_fills_per_day": st.mean(r["fills_stk"] for r in rs),
                "opt_contracts_per_day": st.mean(r["opt_contracts"] for r in rs),
                "opt_reversed_per_day": st.mean(r["opt_reversed"] for r in rs),
                "stk_shares_per_day": st.mean(r["stk_shares"] for r in rs),
                "stk_reversed_per_day": st.mean(r["stk_reversed"] for r in rs),
                "eligible_plans_per_day": st.mean(r["eligible"] for r in rs),
                "plan_flips_per_day": st.mean(r["plan_flips"] for r in rs),
                "mean_modeled_score_held": st.mean(r["mean_modeled_score_held"] for r in rs),
                "nav_rv_1m": st.mean(r["nav_rv_1m"] for r in rs),
                "expected_miss_rv": st.mean(r["expected_miss_rv"] for r in rs),
                "g_open": {d: round(days[d]["g_open"], 0) for d in sorted(days)},
                "opt_fill_cost_frac": st.mean([r["opt_fill_cost_frac"] for r in rs if r["opt_fill_cost_frac"] is not None])
                if any(r["opt_fill_cost_frac"] is not None for r in rs)
                else None,
                "hard_stops": sum(r["hard_stopped"] for r in rs),
                "pending_minutes_per_day": st.mean(r["pending_minutes"] for r in rs),
                "paired_vs_B900_days": common if v != "A_B900" else [],
                "paired_miss_diff_mean": st.mean(paired_miss) if paired_miss else None,
                "paired_miss_diff": [round(x, 0) for x in paired_miss],
                "paired_cost_diff_mean": st.mean(paired_cost) if paired_cost else None,
                "paired_modeled_held_diff_mean": st.mean(paired_model) if paired_model else None,
                "paired_nav_rv_diff_mean": st.mean(paired_rv) if paired_rv else None,
                "paired_expected_miss_rv_diff_mean": st.mean(paired_em) if paired_em else None,
            }
        )
    for o in out:
        print(json.dumps({k: (round(x, 2) if isinstance(x, float) else x) for k, x in o.items()}))


if __name__ == "__main__":
    main()

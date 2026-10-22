"""Final comparison for the spread_model lane on identical sessions.

python final_report.py VARIANT [VARIANT ...]   (variant = run-name prefix without the _MMDD suffix)
Prints, per variant, metrics over (a) all its sessions and (b) the sessions every listed variant ran.
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import summarize  # noqa: E402

RUNS = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "spread_model" / "runs"
OPTION_HEDGES = [str(i) for i in range(1008, 1018)]


def target_wander(report):
    """Option-hedge target path over consecutive FULL OPTIMAL solves: total variation, net change, reversals."""
    full = sorted(
        (d for d in report["decisions"] if d.get("optimization_scope") == "FULL" and d.get("status") == "OPTIMAL"),
        key=lambda d: d["at"],
    )
    tv = net = rev = 0
    for i in OPTION_HEDGES:
        seq = [d["quantities"].get(i, 0) for d in full]
        if len(seq) < 2:
            continue
        steps = [b - a for a, b in zip(seq, seq[1:]) if b != a]
        tv += sum(abs(s) for s in steps)
        net += abs(seq[-1] - seq[0])
        rev += sum(1 for a, b in zip(steps, steps[1:]) if a * b < 0)
    return tv, net, rev


def load(variant):
    out = {}
    for d in sorted(RUNS.glob(f"{variant}_*")):
        rep = d / "report.json"
        if not rep.exists() or "skipped" in json.loads(rep.read_text()) or not (d / "log.jsonl").exists():
            continue
        if "_".join(d.name.split("_")[:-1]) != variant:
            continue
        s = summarize(d)
        s["wander"] = target_wander(json.loads(rep.read_text()))
        out[s["day"]] = s
    return out


def block(days, rows, base=None):
    rs = [rows[d] for d in days]
    if not rs:
        return None
    misses = [r["miss"] for r in rs]
    res = {
        "n": len(rs),
        "mean_miss": st.mean(misses),
        "median_miss": st.median(misses),
        "cost_day": st.mean(r["cost"] for r in rs),
        "spread_day": st.mean(r["spread_paid"] for r in rs),
        "fees_day": st.mean(r["fees"] for r in rs),
        "opt_spread_day": st.mean(r["opt_spread"] for r in rs),
        "fills_day": st.mean(r["fills_opt"] + r["fills_stk"] for r in rs),
        "opt_fills_day": st.mean(r["fills_opt"] for r in rs),
        "stk_fills_day": st.mean(r["fills_stk"] for r in rs),
        "opt_contracts_day": st.mean(r["opt_contracts"] for r in rs),
        "opt_reversed_day": st.mean(r["opt_reversed"] for r in rs),
        "stk_shares_day": st.mean(r["stk_shares"] for r in rs),
        "stk_reversed_day": st.mean(r["stk_reversed"] for r in rs),
        "eligible_day": st.mean(r["eligible"] for r in rs),
        "full_solves_day": st.mean(r["full_solves"] for r in rs),
        "target_tv_day": st.mean(r["wander"][0] for r in rs),
        "target_net_day": st.mean(r["wander"][1] for r in rs),
        "target_rev_day": st.mean(r["wander"][2] for r in rs),
        "expected_miss_rv": st.mean(r["expected_miss_rv"] for r in rs),
        "nav_rv_1m": st.mean(r["nav_rv_1m"] for r in rs),
        "mean_modeled_score_held": st.mean(r["mean_modeled_score_held"] for r in rs),
        "hard_stops": sum(r["hard_stopped"] for r in rs),
        "per_day_miss": {d[5:]: round(rows[d]["miss"]) for d in days},
    }
    if base is not None:
        diffs = [rows[d]["miss"] - base[d]["miss"] for d in days]
        res["paired_miss_diff_vs_B900"] = [round(x) for x in diffs]
        res["paired_miss_diff_mean"] = st.mean(diffs)
        if len(diffs) > 1:
            res["paired_miss_diff_se"] = st.stdev(diffs) / len(diffs) ** 0.5
        res["paired_em_diff_mean"] = st.mean(rows[d]["expected_miss_rv"] - base[d]["expected_miss_rv"] for d in days)
        res["paired_modeled_held_diff_mean"] = st.mean(
            rows[d]["mean_modeled_score_held"] - base[d]["mean_modeled_score_held"] for d in days
        )
    return res


def rnd(x):
    if isinstance(x, float):
        return round(x, 1)
    if isinstance(x, dict):
        return {k: rnd(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [rnd(v) for v in x]
    return x


def main():
    variants = sys.argv[1:]
    data = {v: load(v) for v in variants}
    base = data.get("A_B900")
    common = sorted(set.intersection(*(set(x) for x in data.values()))) if data else []
    print("common sessions:", common)
    for v, rows in data.items():
        b = base if (base is not None and v != "A_B900") else None
        own = sorted(rows)
        own_b = [d for d in own if b is None or d in b]
        print(json.dumps(rnd({"variant": v, "sessions": own,
                          "all": block(own_b, rows, b),
                          "common": block(common, rows, b)}),
                         default=lambda x: round(x, 2) if isinstance(x, float) else str(x)))


if __name__ == "__main__":
    main()

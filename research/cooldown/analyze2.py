"""Cooldown lane, extra metrics: residual risk from per-minute NAV ticks, plans executed, stock/option reversals,
paired session comparisons with bootstrap CIs.  python analyze2.py [runs_dir] [variant ...]"""

import json
import os
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

RUNS = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "cooldown" / "runs")
BASE = "touch_o900_t100"
rng = np.random.default_rng(7)


def flips(fills, kind):
    by = defaultdict(list)
    for f in fills:
        if f["kind"] == kind:
            by[f["con_id"]].append(f["qty"])
    n, rt = 0, 0
    for seq in by.values():
        s = [q > 0 for q in seq]
        n += sum(a != b for a, b in zip(s, s[1:], strict=False))
        rt += min(sum(q for q in seq if q > 0), -sum(q for q in seq if q < 0))
    return n, rt


def run_metrics(d):
    x = json.loads((d / "summary.json").read_text())
    ticks = []
    for line in open(d / "log.jsonl"):
        if '"kind": "tick"' in line:
            r = json.loads(line)
            ticks.append((r["at"], r["nav"]))
    ticks.sort()
    nav = np.array([v for _, v in ticks])
    target = x["target"]
    # Close jump: tick NAV at 16:00 is at last mids; the scored value uses official-close marks + accrual.
    path = np.append(nav, x["close_nav_model"])
    inc5 = np.diff(path[::5]) if len(path) > 5 else np.diff(path)
    qv5 = float(np.sum(inc5 ** 2))
    gap = path - target
    last_hour = gap[-61:]
    fills = x["fills"]
    dec_at = sorted(datetime.fromisoformat(dd["at"]) for dd in x["decisions"] if dd.get("at"))
    plans = defaultdict(set)
    for f in fills:
        k = bisect_right(dec_at, datetime.fromisoformat(f["at"])) - 1
        plans[f["kind"]].add(k)
    of, ort = flips(fills, "OPT")
    sf, srt = flips(fills, "STK")
    spread = sum(f["spread_cost"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    return {
        "session": x["session"], "miss": x["miss"], "abs": abs(x["miss"]), "spread": spread, "fees": fees,
        "cost": spread + fees, "abs_ex": abs(x["miss"] + spread + fees),
        "fills": len(fills), "opt_fills": sum(f["kind"] == "OPT" for f in fills),
        "stk_fills": sum(f["kind"] == "STK" for f in fills),
        "contracts": sum(abs(f["qty"]) for f in fills if f["kind"] == "OPT"),
        "shares": sum(abs(f["qty"]) for f in fills if f["kind"] == "STK"),
        "opt_plans": len(plans["OPT"]), "stk_plans": len(plans["STK"]),
        "opt_flips": of, "opt_rt": ort, "stk_flips": sf, "stk_rt": srt,
        "rv5": np.sqrt(qv5), "rms_gap_last_hour": float(np.sqrt(np.mean(last_hour ** 2))),
        "mean_abs_gap": float(np.mean(np.abs(gap))),
        "stops": len((x.get("operator") or {}).get("stops", [])),
        "paused_min": (x.get("operator") or {}).get("paused_seconds", 0) / 60,
        "hedges": x["hedges"],
    }


def boot_ci(diff, n=20000):
    if len(diff) < 2:
        return float("nan"), float("nan")
    idx = rng.integers(0, len(diff), size=(n, len(diff)))
    m = diff[idx].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main():
    want = sys.argv[2:]
    rows = defaultdict(dict)
    for s in sorted(RUNS.glob("*/summary.json")):
        name = s.parent.name
        if name.startswith("warm_"):
            continue
        v, session = name.rsplit("__", 1)
        if want and v not in want:
            continue
        rows[v][session] = run_metrics(s.parent)
    variants = sorted(rows, key=lambda v: (v.split("_t")[1], v))
    keys = ["abs", "cost", "spread", "fees", "fills", "opt_plans", "stk_plans", "contracts", "shares", "opt_flips",
            "opt_rt", "stk_flips", "stk_rt", "rv5", "mean_abs_gap", "stops", "paused_min"]
    print("per-variant means over the sessions each variant has (n); |miss| median / sd in brackets")
    print("variant\tn\t|miss| mean [med, sd]\t" + "\t".join(keys[1:]))
    for v in variants:
        r = list(rows[v].values())
        a = {k: np.array([x[k] for x in r], dtype=float) for k in keys}
        print(f"{v}\t{len(r)}\t{a['abs'].mean():.0f} [{np.median(a['abs']):.0f}, "
              f"{a['abs'].std(ddof=1) if len(r) > 1 else 0:.0f}]\t"
              + "\t".join(f"{a[k].mean():.1f}" for k in keys[1:]))
    print(f"\npaired vs {BASE} on common sessions: mean diff [bootstrap 95% CI], wins = sessions where variant lower")
    print("variant\tn\td|miss|\td(|miss|+cost)*\tdcost\tdRV5 (residual risk)\td mean|gap|")
    for v in variants:
        if v == BASE or BASE not in rows:
            continue
        common = sorted(set(rows[v]) & set(rows[BASE]))
        if not common:
            continue

        def d(k, v=v, common=common):
            return np.array([rows[v][s][k] - rows[BASE][s][k] for s in common])

        out = []
        for k in ("abs", "abs_ex_cost", "cost", "rv5", "mean_abs_gap"):
            if k == "abs_ex_cost":
                dd = np.array([(rows[v][s]["abs_ex"] + rows[v][s]["cost"]) - (rows[BASE][s]["abs_ex"] + rows[BASE][s]["cost"])
                               for s in common])
            else:
                dd = d(k)
            lo, hi = boot_ci(dd)
            out.append(f"{dd.mean():+.0f} [{lo:+.0f},{hi:+.0f}] {int((dd < 0).sum())}/{len(dd)}")
        print(f"{v}\t{len(common)}\t" + "\t".join(out))
    print("* |miss ex costs| + costs: the hedge error before trading costs, plus the costs, so costs count fully")
    # balanced tables
    for t in ("t100", "t50"):
        core = [f"touch_o{o}_{t}" for o in (60, 120, 300, 900)]
        core = [c for c in core if c in rows]
        if len(core) < 2:
            continue
        common = sorted(set.intersection(*(set(rows[c]) for c in core)))
        print(f"\nbalanced {t}: {len(common)} sessions {[s[5:] for s in common]}")
        print("variant\t|miss| mean\tmedian\tsd\tcost/d\t|miss|+cost\tRV5\tctrs/d\tshares/d\topt plans/d\tfills/d")
        for c in core:
            r = [rows[c][s] for s in common]
            ab = np.array([x["abs"] for x in r])
            print(f"{c}\t{ab.mean():.0f}\t{np.median(ab):.0f}\t{ab.std(ddof=1) if len(r) > 1 else 0:.0f}\t"
                  f"{np.mean([x['cost'] for x in r]):.0f}\t{np.mean([x['abs_ex'] + x['cost'] for x in r]):.0f}\t"
                  f"{np.mean([x['rv5'] for x in r]):.0f}\t{np.mean([x['contracts'] for x in r]):.1f}\t"
                  f"{np.mean([x['shares'] for x in r]):.0f}\t{np.mean([x['opt_plans'] for x in r]):.1f}\t"
                  f"{np.mean([x['fills'] for x in r]):.1f}")
    # threshold pairs
    print("\nthreshold 50 vs 100 at the same cooldown (paired)")
    for o in (60, 120, 300, 900):
        a, b = rows.get(f"touch_o{o}_t50", {}), rows.get(f"touch_o{o}_t100", {})
        common = sorted(set(a) & set(b))
        if not common:
            continue
        dd = np.array([a[s]["abs"] - b[s]["abs"] for s in common])
        dc = np.array([a[s]["cost"] - b[s]["cost"] for s in common])
        dr = np.array([a[s]["rv5"] - b[s]["rv5"] for s in common])
        lo, hi = boot_ci(dd)
        print(f"o{o}\tn={len(common)}\td|miss| {dd.mean():+.0f} [{lo:+.0f},{hi:+.0f}] wins {int((dd < 0).sum())}"
              f"\tdcost {dc.mean():+.0f}\tdRV5 {dr.mean():+.0f}\tdshares {np.mean([a[s]['shares'] - b[s]['shares'] for s in common]):+.0f}")
    print("\nper-session signed miss")
    sessions = sorted({s for v in rows.values() for s in v})
    print("variant\t" + "\t".join(s[5:] for s in sessions))
    for v in variants:
        print(v + "\t" + "\t".join(f"{rows[v][s]['miss']:+.0f}" if s in rows[v] else "-" for s in sessions))
    print("\nper-session residual risk RV5 ($, sqrt of summed squared 5-min NAV changes 09:45-close)")
    for v in variants:
        print(v + "\t" + "\t".join(f"{rows[v][s]['rv5']:.0f}" if s in rows[v] else "-" for s in sessions))
    json.dump({v: rows[v] for v in variants}, open(RUNS / "rows2.json", "w"), default=str, indent=0)


if __name__ == "__main__":
    main()

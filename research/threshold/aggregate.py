"""Aggregate analyze_thr output across runs: python aggregate.py runs/*_t*/ > summary.json (+ table on stderr)."""

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_thr import analyze  # noqa: E402

runs = [analyze(Path(p)) for p in sys.argv[1:] if (Path(p) / "report.json").exists()]
by_path = {}
for r in runs:
    by_path.setdefault(r["run"].rsplit("_t", 1)[0], {})[int(r["threshold"])] = r

COLS = [
    ("decisions", lambda r: r["decisions"]),
    ("eligible", lambda r: r["eligible"]),
    ("elig_corr", lambda r: r["eligible_corrective"]),
    ("elig_<100", lambda r: r["eligible_below_100_band"]),
    ("thr_binds", lambda r: r["score_threshold_binding"]),
    ("plans", lambda r: r["plans"]),
    ("plans<100", lambda r: r["plans_small_lt100"]),
    ("orders", lambda r: r["hedge_orders"]),
    ("opt_ct", lambda r: r["hedge"]["option_contracts"]),
    ("shares", lambda r: r["hedge"]["shares"]),
    ("cost", lambda r: r["hedge"]["total"]),
    ("spread", lambda r: r["hedge"]["spread"]),
    ("fees", lambda r: r["hedge"]["fees"]),
    ("burn_cost", lambda r: r["burn"]["total"]),
    ("opt_cd", lambda r: r["opt_cooldowns"]),
    ("opt_blk", lambda r: r["opt_cooldown_blocks"]),
    ("blk_small", lambda r: r["opt_blocks_by_small_plan"]),
    ("gap1539", lambda r: r["gap_1539"]),
    ("miss16", lambda r: r["close_miss"]),
]

lines = []
header = f"{'run':<14}" + "".join(f"{c:>10}" for c, _ in COLS)
lines.append(header)
for path in sorted(by_path):
    for thr in sorted(by_path[path], reverse=True):
        r = by_path[path][thr]
        lines.append(f"{r['run']:<14}" + "".join(f"{str(f(r)):>10}" for _, f in COLS))
for thr in (100, 50, 30):
    rs = [by_path[p][thr] for p in by_path if thr in by_path[p]]
    if not rs:
        continue
    med = []
    for _, f in COLS:
        values = [f(r) for r in rs if f(r) is not None]
        med.append(round(statistics.median(values), 2) if values else None)
    lines.append(f"{'median t' + str(thr):<14}" + "".join(f"{str(v):>10}" for v in med))
print("\n".join(lines), file=sys.stderr)

diffs = {}
for path, d in by_path.items():
    if 100 in d:
        for thr in (50, 30):
            if thr in d and d[thr]["close_miss"] is not None and d[100]["close_miss"] is not None:
                diffs.setdefault(thr, []).append(
                    {"path": path, "miss_minus_t100": round(d[thr]["close_miss"] - d[100]["close_miss"], 2)}
                )
print(json.dumps({"runs": runs, "miss_vs_t100": diffs}, default=str))

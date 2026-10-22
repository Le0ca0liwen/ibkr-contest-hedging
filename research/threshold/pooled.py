"""Pooled plan-level modeled vs realized, and model-calibration of the 16:00 miss.

python pooled.py runs/<PATH>_t<THR> ...
"""

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_thr import analyze  # noqa: E402

runs = [analyze(Path(p)) for p in sys.argv[1:] if (Path(p) / "report.json").exists()]

# ---- unique executed plans (a plan shared by several thresholds on one path is counted once,
#      unless its realized effect differs because the close differs: then keep the t100 copy)
seen = {}
for r in sorted(runs, key=lambda r: -r["threshold"]):
    path = r["run"].rsplit("_t", 1)[0]
    for p in r["plans_detail"]:
        key = (path, p["at"], p["scope"], round(p["modeled"] or 0, 1), p["shares"], p["option_contracts"])
        if key not in seen:
            seen[key] = {**p, "path": path, "run": r["run"], "run_threshold": r["threshold"]}
plans = list(seen.values())

buckets = [(-1e9, 50), (50, 100), (100, 300), (300, 1000), (1000, 1e9)]
print("bucket(modeled total)   n  corr  sum_modeled  sum_mod_closing  sum_realized  mean_realized  sd_realized  sum_rcost  sum_mcost_fills")
for lo, hi in buckets:
    group = [p for p in plans if lo <= (p["modeled"] or 0) < hi and p["realized"] is not None]
    if not group:
        continue
    real = [p["realized"] for p in group]
    print(
        f"[{max(lo, 0):>6.0f},{min(hi, 99999):>6.0f})  {len(group):>4} {sum(bool(p['corrective']) for p in group):>5}"
        f" {sum(p['modeled'] or 0 for p in group):>12.1f} {sum(p['modeled_closing'] or 0 for p in group):>16.1f}"
        f" {sum(real):>13.1f} {statistics.mean(real):>14.1f} {statistics.pstdev(real):>12.1f}"
        f" {sum(p['realized_cost'] for p in group):>10.1f} {sum(p['modeled_cost_fills'] for p in group):>16.1f}"
    )
small = [p for p in plans if (p["modeled"] or 0) < 100 and not p["corrective"]]
print("\nplans with modeled improvement < $100 (non-corrective):")
for p in sorted(small, key=lambda p: (p["path"], p["at"])):
    print(f"  {p['run']:<10} {p['at']} {p['scope']:<6} modeled {p['modeled']:.1f} (closing {p['modeled_closing'] or 0:.1f}, "
          f"overnight {p['modeled_overnight'] or 0:.1f}) thr {p['threshold']:.1f} sh {p['shares']} opt {p['option_contracts']} "
          f"rcost {p['realized_cost']} mcost {p['modeled_cost_fills']} realized {p['realized']}")


# ---- calibration: the model's own E|closing gap| (old_closing of the current book) vs realized |gap|
def model_expected(run_dir, hhmm):
    rows = [json.loads(line) for line in (run_dir / "log.jsonl").read_text().splitlines() if '"solve"' in line]
    cand = [r for r in rows if r["at"][11:16] <= hhmm and r.get("old_closing") is not None]
    return cand[-1]["old_closing"] if cand else None


print("\ncalibration (model E|16:00 gap| for the book held at time T vs realized |gap| at 16:00):")
print(f"{'run':<12}{'E@10:00':>10}{'E@12:00':>10}{'E@14:00':>10}{'E@15:30':>10}{'realized':>10}  mode")
ratios = []
for r in runs:
    d = Path("runs") / r["run"]
    vals = [model_expected(d, h) for h in ("10:00", "12:00", "14:00", "15:30")]
    print(f"{r['run']:<12}" + "".join(f"{(v or 0):>10.0f}" for v in vals) + f"{r['close_miss'] or 0:>10.0f}  {r['mode']}")
    if vals[1] and r["mode"] == "PAPER":
        ratios.append(r["close_miss"] / vals[1])
if ratios:
    print("realized / model-expected (from 12:00, PAPER runs only): median", round(statistics.median(ratios), 2), [round(x, 2) for x in ratios])

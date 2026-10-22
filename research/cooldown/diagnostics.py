"""Execution diagnostics across runs: realized vs modeled spread cost, stalled eligible plans, hard-stop causes."""
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

RUNS = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "cooldown" / "runs")
by_kind = defaultdict(list)
stall = defaultdict(list)
stop_causes = Counter()
urg = Counter()
per_variant = defaultdict(lambda: defaultdict(list))
for s in sorted(RUNS.glob("*/summary.json")):
    name = s.parent.name
    x = json.loads(s.read_text())
    variant = name.rsplit("__", 1)[0]
    for f in x["fills"]:
        half = (f["ask"] - f["bid"]) / 2
        mult = 100 if f["kind"] == "OPT" else 1
        paid = (f["price"] - f["mid"]) * (1 if f["qty"] > 0 else -1)  # per unit, >0 = cost
        by_kind[f["kind"]].append((paid, half, abs(f["qty"]) * mult))
    for st in (x.get("operator") or {}).get("stops", []):
        stop_causes[st["health"].split(";")[0][:70]] += 1
    lines = [json.loads(line) for line in open(s.parent / "log.jsonl")]
    submits = [r for r in lines if r["kind"] == "submit"]
    for r in submits:
        urg[(r.get("urgency"), "OPT" if r["con_id"] > 1000 else "STK")] += 1
    states = [r for r in lines if r["kind"] == "state"]
    no_slice = sum("No economical execution slice" in r["health"] for r in states)
    eligible = [d for d in x["decisions"] if d.get("trade_eligible")]
    full_eligible = [d for d in eligible if d.get("optimization_scope") == "FULL"]
    opt_fills = [f for f in x["fills"] if f["kind"] == "OPT"]
    per_variant[variant]["no_slice"].append(no_slice)
    per_variant[variant]["eligible_full"].append(len(full_eligible))
    per_variant[variant]["eligible"].append(len(eligible))
    per_variant[variant]["opt_submits"].append(sum(r["con_id"] > 1000 for r in submits))
    per_variant[variant]["opt_fills"].append(len(opt_fills))
    per_variant[variant]["cancels"].append(sum(r["kind"] == "cancel" for r in lines))
    per_variant[variant]["replaces"].append(sum(r["kind"] == "replace" for r in lines))

print("realized execution cost per unit vs quoted half-spread (fill price - mid, signed as cost)")
for k, rows in by_kind.items():
    a = np.array(rows)
    w = a[:, 2]
    print(f"  {k}: fills {len(a)}  paid/half-spread (unit-weighted) {np.sum(a[:, 0] * w) / np.sum(a[:, 1] * w):.2f}  "
          f"mean paid ${np.mean(a[:, 0]):.3f}/unit  mean half-spread ${np.mean(a[:, 1]):.3f}/unit  "
          f"share at/through touch {np.mean(a[:, 0] >= a[:, 1] - 1e-9):.2f}")
print("  (optimizer charges (0.5 + slippage 0.1) x spread = 1.2 x half-spread per unit, plus commissions)")
print("\nhard-stop causes:", dict(stop_causes))
print("submits by (urgency, kind):", dict(urg))
print("\nper variant (mean per day): eligible decisions / eligible FULL / 'no economical slice' states / option submits"
      " / option fills / cancels / replaces")
for v, d in sorted(per_variant.items()):
    print(f"  {v}\tn={len(d['eligible'])}\t" + "\t".join(
        f"{np.mean(d[k]):.1f}" for k in ("eligible", "eligible_full", "no_slice", "opt_submits", "opt_fills", "cancels",
                                          "replaces")))

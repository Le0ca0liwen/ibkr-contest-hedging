"""Summarize one sim run directory: python analyze.py <run_dir>."""

import json
import sys
from datetime import datetime
from pathlib import Path

run = Path(sys.argv[1])
rows = [json.loads(line) for line in (run / "log.jsonl").read_text().splitlines()]
report = json.loads((run / "report.json").read_text()) if (run / "report.json").exists() else {}
manifest = next(r for r in rows if r["kind"] == "manifest")
mandated = {int(k) for k in manifest["mandatory"]}
day = rows[-1].get("at", "")[:10] if rows else ""


def t(value):
    return datetime.fromisoformat(value)


def hm(value):
    return t(value).strftime("%H:%M:%S.%f")[:-3]


ticks = [r for r in rows if r["kind"] == "tick"]
print("== run", run.name, "ticks", len(ticks))
base = next((r for r in rows if r["kind"] == "baseline"), {})
print("baseline", {k: base.get(k) for k in ("nav0", "d0", "target", "surplus")})
end = next((r for r in rows if r["kind"] == "end"), {})
print("end", end)

print("\n-- state changes (mode / burner / reason / health)")
for r in rows:
    if r["kind"] == "state":
        print(hm(r["at"]), r["mode"], r["burner"], "|", r["reason"][:110], "|", r["health"][:110])

print("\n-- submits")
submits = [r for r in rows if r["kind"] == "submit"]
for r in submits:
    print(
        hm(r["at"]), r["label"], r["qty"], r["tif"], r["type"], r["urgency"], "limit", r["limit"],
        "burn_open" if r["burn_open"] else "", "MANDATED!" if r["mandated"] else "",
    )
bad = [r for r in submits if r["con_id"] == 30 or r["mandated"]]
print("SPY shares / mandated submits:", len(bad))
burn = [r for r in submits if r["urgency"] == "BURN"]
hedge = [r for r in submits if r["urgency"] != "BURN"]
print("burn legs", len(burn), "hedger orders", len(hedge))
print("hedger orders while a burn cycle open:", [hm(r["at"]) for r in hedge if r["burn_open"]])
if day:
    pass
late_stock = [r for r in hedge if r["sec_type"] == "STK" and t(r["at"]).strftime("%H:%M:%S") >= "15:58:00"]
late_opt = [r for r in submits if r["sec_type"] == "OPT" and t(r["at"]).strftime("%H:%M:%S") >= "15:45:00"]
late_burn_entry = [r for r in burn if r["qty"] > 0 and t(r["at"]).strftime("%H:%M:%S") >= "15:56:00"]
late_burn_exit = [r for r in burn if r["qty"] < 0 and t(r["at"]).strftime("%H:%M:%S") >= "15:57:45"]
print("late stock hedges", len(late_stock), "late options", len(late_opt), "late burn entries", len(late_burn_entry),
      "late burn exits", len(late_burn_exit))

print("\n-- solves (sim time, wall s, status, stock_only, current->expected)")
solves = [r for r in rows if r["kind"] == "solve"]
for r in solves:
    print(hm(r["at"]), r["wall_s"], r["status"], "stock" if r["stock_only"] else "full", r.get("strategy"),
          round(r["current"], 1) if r["current"] is not None else None, "->",
          round(r["expected"], 1) if r["expected"] is not None else None)
if solves:
    print("max solve wall", max(r["wall_s"] for r in solves), "n", len(solves),
          "statuses", {s: sum(r["status"] == s for r in solves) for s in {r["status"] for r in solves}})
builds = [r["wall_s"] for r in rows if r["kind"] == "scenario_build"]
if builds:
    print("scenario builds", len(builds), "max", max(builds), "mean", round(sum(builds) / len(builds), 3))

print("\n-- burner gap over time (every 30 s when evaluated)")
last = None
for r in ticks:
    stamp = t(r["at"])
    if r["gap_median"] is not None and (last is None or (stamp - last).total_seconds() >= 30):
        last = stamp
        print(hm(r["at"]), "nav", round(r["nav"], 2), "nav_est", r["nav_estimate"] and round(r["nav_estimate"], 2),
              "target", r["target"] and round(r["target"], 2), "median", round(r["gap_median"], 2),
              "engaged", r["engaged"], r["burner"], "| pos", r["positions"])

results = report.get("burn_results", [])
results = sorted(results, key=lambda r: r["planned_at"])
print("\n-- burn cycle results", len(results))
total_real, total_exp = 0.0, 0.0
prev_finish = None
overlaps = 0
for r in results:
    total_real += r["realized_loss"]
    total_exp += r["expected_loss"]
    if prev_finish and r["planned_at"] < prev_finish:
        overlaps += 1
    prev_finish = r["finished_at"]
    print(hm(r["planned_at"]), "->", hm(r["finished_at"]), r["status"], r["reason"][:50], r["label"],
          "q", r["planned_quantity"], "in", r["entry_filled"], "out", r["exit_filled"], "resid", r["residual"],
          "exp", round(r["expected_loss"], 3), "real", round(r["realized_loss"], 3), "fees", r["fees"],
          "cyc_s", round(r["cycle_seconds"], 1), "exits", r["exit_attempts"])
print("cycles", len(results), "transmitted", sum(r["transmitted"] for r in results), "overlaps", overlaps,
      "expected", round(total_exp, 2), "realized", round(total_real, 2),
      "statuses", {s: sum(r["status"] == s for r in results) for s in {r["status"] for r in results}})
decisions = report.get("burn_decisions", [])
print("burn_decision records", len(decisions), "modes", {m: sum(d.get("mode") == m for d in decisions) for m in
                                                        {d.get("mode") for d in decisions}},
      "would_trade", sum(bool(d.get("would_trade")) for d in decisions))
print("burn intents", sum(1 for i in report.get("intents", []) if i.get("purpose") == "burn"))
print("\n-- final positions", report.get("final_positions"))
print("final nav", report.get("final_nav"), "mode", report.get("mode"), "|", report.get("health"))
for d in report.get("ledger", {}).get("days", [])[:2]:
    print("ledger", d)
print("ledger total", report.get("ledger", {}).get("total"))
for c in report.get("closing_capture", []):
    print("capture", {k: c.get(k) for k in ("date", "status", "nav", "observed_at", "phase", "source")})
print("\n-- alerts")
for a in report.get("alerts", [])[::-1]:
    print(a.get("at"), a.get("severity"), a.get("message", "")[:220])
print("\n-- task exceptions", [r for r in rows if r["kind"] == "task_exception"])

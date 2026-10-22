"""Job runner: per session a 09:30-09:45 warm-up (default policy), then every variant 09:45-16:00 from it.

python runner.py --workers 8 [--sessions ...] [--fill touch]
Resumable: a job whose summary.json exists is skipped.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
LANE = RESEARCH_ROOT / "cooldown"
PY = os.environ.get("PYTHON") or sys.executable
SESSIONS = ["2026-10-20", "2026-10-15", "2026-10-21", "2026-10-13", "2026-10-08", "2026-10-16", "2026-10-09", "2026-10-14",
            "2026-10-19", "2026-10-12"]
VARIANTS = [(60, 100), (60, 50), (120, 100), (120, 50), (300, 100), (300, 50), (900, 100), (900, 50)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--sessions", nargs="*", default=SESSIONS)
    p.add_argument("--fill", default="touch")
    p.add_argument("--runs", default=str(LANE / "runs"))
    p.add_argument("--solver-scale", default="3")
    p.add_argument("--market-dir", default=str(LANE / "market"))
    p.add_argument("--variants", default=None, help="comma list like 60:100,900:100")
    args = p.parse_args()
    variants = VARIANTS if not args.variants else [tuple(int(x) for x in v.split(":")) for v in args.variants.split(",")]
    runs = Path(args.runs)
    runs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "PYTHONWARNINGS": "ignore"}
    jobs = []
    for s in args.sessions:
        jobs.append({"name": f"warm_{args.fill}__{s}", "session": s, "needs": None,
                     "argv": ["--start", "09:30:00", "--end", "09:45:00"]})
    # Core comparison first (every cooldown at the first threshold on every session), then the next threshold.
    for threshold in dict.fromkeys(t for _, t in variants):
        for s in args.sessions:
            warm = f"warm_{args.fill}__{s}"
            for o, t in variants:
                if t != threshold:
                    continue
                jobs.append({"name": f"{args.fill}_o{o}_t{t}__{s}", "session": s, "needs": warm,
                             "argv": ["--start", "09:45:00", "--end", "16:00:00",
                                      "--policy", json.dumps({"option_seconds": o, "score_threshold": t}),
                                      "--state", str(runs / warm / "state.json")]})
    # Early paired threshold comparison: complete the second-threshold sets of these sessions right after
    # the first sessions' core runs.
    early = {"2026-10-20", "2026-10-13"}
    core = [j for j in jobs if j["needs"] and j["name"].split("_t")[1].startswith(str(variants[0][1]) + "__")]
    second = [j for j in jobs if j["needs"] and j not in core]
    warms = [j for j in jobs if not j["needs"]]
    first_core = [j for j in core if j["session"] in args.sessions[:3]]
    jobs = (warms + first_core + [j for j in second if j["session"] in early]
            + [j for j in core if j not in first_core] + [j for j in second if j["session"] not in early])
    log = open(runs / "runner.log", "a")

    def done(name):
        return (runs / name / "summary.json").exists()

    def external(name):  # started by an earlier runner and still writing
        f = runs / name / "log.jsonl"
        return f.exists() and not done(name) and time.time() - f.stat().st_mtime < 180

    running = {}
    pending = [j for j in jobs if not done(j["name"]) and not external(j["name"])]
    outside = {j["name"] for j in jobs if external(j["name"])}
    t0 = time.time()
    while pending or running or outside:
        for name, (proc, started) in list(running.items()):
            if proc.poll() is not None:
                log.write(f"{time.strftime('%H:%M:%S')} done {name} rc={proc.returncode} "
                          f"wall={time.time() - started:.0f}s\n")
                log.flush()
                del running[name]
        failed = {name for name in (j["name"] for j in jobs)
                  if (runs / name / "log.jsonl").exists() and not done(name) and name not in running
                  and name not in {j["name"] for j in pending}}
        outside = {n for n in outside if external(n)}
        for j in list(pending):
            if len(running) + len(outside) >= args.workers:
                break
            if j["needs"] and not done(j["needs"]):
                if j["needs"] in failed:
                    pending.remove(j)
                continue
            if not (Path(args.market_dir) / f"{j['session']}.npz").exists():
                continue
            out = runs / j["name"]
            out.mkdir(parents=True, exist_ok=True)
            cmd = [PY, str(LANE / "sim_replay.py"), "--session", j["session"], "--out", str(out), "--fill", args.fill,
                   "--solver-scale", args.solver_scale, "--market-dir", args.market_dir, *j["argv"]]
            proc = subprocess.Popen(cmd, stdout=open(out / "stdout.txt", "w"), stderr=subprocess.STDOUT, env=env)
            running[j["name"]] = (proc, time.time())
            pending.remove(j)
            log.write(f"{time.strftime('%H:%M:%S')} start {j['name']}\n")
            log.flush()
        time.sleep(5)
    log.write(f"{time.strftime('%H:%M:%S')} ALL DONE in {time.time() - t0:.0f}s\n")
    log.flush()


if __name__ == "__main__":
    sys.exit(main())

"""Run a queue of replay jobs with N workers: python batch.py N jobs/jobsA.json"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
N = int(sys.argv[1]); jobs = json.loads(Path(sys.argv[2]).read_text())
LANE = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "spread_model"
RUNS = LANE / "runs"
PY = os.environ.get("PYTHON") or sys.executable
LOGS = RUNS / "logs"; LOGS.mkdir(parents=True, exist_ok=True)
def run(job):
    name = job["name"]
    if (RUNS / name / "report.json").exists() and not job.get("force"):
        return name, "skip"
    cmd = [PY, "replay_sim.py", "--name", name, "--day", job["day"], "--repo", job.get("repo", "base"),
           "--policy", json.dumps(job.get("policy", {})), "--execution", json.dumps(job.get("execution", {})), "--fill", job.get("fill", "poisson")]
    t = time.time()
    with open(LOGS / f"{name}.out", "w") as out, open(LOGS / f"{name}.err", "w") as err:
        rc = subprocess.call(cmd, stdout=out, stderr=err, cwd=str(LANE))
    with open(LOGS / "batch.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {name} rc={rc} {time.time()-t:.0f}s\n")
    return name, rc
with ThreadPoolExecutor(N) as ex:
    for r in ex.map(run, jobs):
        print(r, flush=True)

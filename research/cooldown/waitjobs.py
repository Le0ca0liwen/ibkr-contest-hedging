"""Wait until a new runs/*/summary.json appears (or the time limit passes): python waitjobs.py [SECONDS]"""
import glob, os, sys, time
from pathlib import Path

RUNS = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "cooldown" / "runs"
n0 = len(glob.glob(str(RUNS / "*" / "summary.json")))
t = time.time()
limit = float(sys.argv[1]) if len(sys.argv) > 1 else 570
while time.time() - t < limit:
    n = len(glob.glob(str(RUNS / "*" / "summary.json")))
    if n > n0:
        break
    time.sleep(15)
print('summaries', n0, '->', n, 'waited', round(time.time() - t))

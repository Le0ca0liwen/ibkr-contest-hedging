"""Launch the contest dashboard: project-local PostgreSQL (database hedge_contest), Mac kept
awake, Moomoo quote codes and daily history refreshed (quote-only, never trades), dashboard
opened. Paper trading then starts by itself only if the configuration (or the dashboard switch)
sets autostart: paper, once every paper activation check passes."""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from local_postgres import start_database

root = Path(__file__).resolve().parents[1]
CONFIG = Path("config/local.contest.yaml")
PYTHON = root / ".venv/bin/python"
MOOMOO_PYTHON = root / ".runtime/moomoo-env/bin/python"
URL = "http://127.0.0.1:8765"


def step(title, command, required=True):
    print(f"\n== {title}", flush=True)
    result = subprocess.run([str(c) for c in command])
    if result.returncode and required:
        sys.exit(f"Stopped: {title} failed. Fix the message above, then double-click the launcher again.")
    if result.returncode:
        print(f"Warning: {title} failed; the dashboard will show what is missing.", flush=True)


def main():
    os.chdir(root)
    sys.path.insert(0, str(root))
    if not CONFIG.exists():
        sys.exit("config/local.contest.yaml is missing; create it with prepare-contest first.")
    step("Validate the contest configuration", [PYTHON, "-m", "hedge.cli", "validate", CONFIG])
    from hedge.config import load_manifest

    manifest = load_manifest(CONFIG)
    print("\n== Start the local PostgreSQL database (hedge_contest)", flush=True)
    env = start_database("hedge_contest")
    print("PostgreSQL database hedge_contest ready (migrations applied)", flush=True)
    step(
        "Write the Moomoo quote-collector list",
        [PYTHON, "-m", "hedge.cli", "moomoo-codes", "--config", CONFIG],
    )
    if manifest.market_data_provider == "moomoo" and manifest.daily_stock_history:
        symbols = sorted(
            {c.symbol for c in manifest.contracts if c.kind == "STK"} | {manifest.beta_benchmark}
        )
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        step(
            "Refresh daily price history from Moomoo (quote-only)",
            [
                MOOMOO_PYTHON,
                "ops/fetch_moomoo_daily.py",
                "--symbols",
                *symbols,
                "--as-of",
                today,
                "--output",
                manifest.daily_stock_history,
            ],
            required=False,
        )
    print(f"\n== Automatic paper start: {manifest.autostart} (the dashboard switch can change it)")
    # Keep the Mac awake for as long as the service runs (exec keeps this process id).
    subprocess.Popen(["/usr/bin/caffeinate", "-dis", "-w", str(os.getpid())])
    subprocess.Popen(
        [
            str(PYTHON),
            "-c",
            "import subprocess,time,urllib.request\n"
            "for _ in range(120):\n"
            "    try:\n"
            f"        urllib.request.urlopen('{URL}/api/state', timeout=1)\n"
            f"        subprocess.run(['/usr/bin/open', '{URL}'], check=False)\n"
            "        break\n"
            "    except OSError:\n"
            "        time.sleep(1)\n",
        ]
    )
    print(f"\n== Starting the contest service on {URL} (Control-C stops it)", flush=True)
    os.execve(
        str(PYTHON),
        [str(PYTHON), "-m", "hedge.cli", "serve", "--config", str(CONFIG), "--port", "8765"],
        env,
    )


if __name__ == "__main__":
    main()

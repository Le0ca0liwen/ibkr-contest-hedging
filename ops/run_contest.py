"""Launch the contest dashboard: project-local PostgreSQL (database hedge_contest), Mac kept
awake, Moomoo quote codes and daily history refreshed (quote-only, never trades), dashboard
opened. Paper trading then starts by itself only if the configuration (or the dashboard switch)
sets autostart: paper, once every paper activation check passes."""

import os
import socket
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from local_postgres import start_database

root = Path(__file__).resolve().parents[1]
CONFIG = Path("config/local.contest.yaml")
PYTHON = root / ".venv/bin/python"
MOOMOO_PYTHON = root / ".runtime/moomoo-env/bin/python"
URL = "http://127.0.0.1:8765"


def step(title, command, required=True, timeout=None):
    print(f"\n== {title}", flush=True)
    try:
        returncode = subprocess.run([str(c) for c in command], timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        if required:
            raise
        returncode = f"{type(exc).__name__}: {exc}"
    if returncode and required:
        sys.exit(f"Stopped: {title} failed. Fix the message above, then double-click the launcher again.")
    if returncode:
        print(f"Warning: {title} failed ({returncode}); the dashboard will show what is missing.", flush=True)


def port_in_use(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def main():
    os.chdir(root)
    sys.path.insert(0, str(root))
    if not CONFIG.exists():
        sys.exit("config/local.contest.yaml is missing; create it with prepare-contest first.")
    if port_in_use(8765):
        sys.exit(
            "Another dashboard (Start Practice, or an earlier Start Contest window) is using port 8765. "
            "Close that window first, then double-click the launcher again."
        )
    step("Validate the contest configuration", [PYTHON, "-m", "hedge.cli", "validate", CONFIG])
    from hedge.config import load_manifest

    manifest = load_manifest(CONFIG)
    print("\n== Start the local PostgreSQL database (hedge_contest)", flush=True)
    env = start_database("hedge_contest")
    print("PostgreSQL database hedge_contest ready (migrations applied)", flush=True)
    # The service writes the Moomoo quote list from this configuration when it starts.
    # Optional: never during the close (the service refuses that too), never blocking the launch
    # for long when OpenD is down; the service retries by itself between 08:30 and 09:29 ET.
    now = datetime.now(ZoneInfo("America/New_York"))
    closing = time(15, 35) <= now.time() <= time(16, 10)
    if closing or not MOOMOO_PYTHON.exists():
        print("\n== Price history refresh skipped (closing window or no Moomoo environment)", flush=True)
    elif manifest.market_data_provider == "moomoo" and manifest.daily_stock_history:
        symbols = sorted(
            {c.symbol for c in manifest.contracts if c.kind == "STK"} | {manifest.beta_benchmark}
        )
        today = now.date().isoformat()
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
            timeout=150,
        )
    print(
        f"\n== Automatic paper start in the configuration: {manifest.autostart}. A choice made with the "
        "dashboard's Auto paper button takes precedence; the Today panel shows the one in effect."
    )
    # Keep the Mac awake for as long as the service runs (exec keeps this process id).
    subprocess.Popen(["/usr/bin/caffeinate", "-dis", "-w", str(os.getpid())])
    subprocess.Popen(
        [
            str(PYTHON),
            "-c",
            "import os,subprocess,time,urllib.request\n"
            "for _ in range(120):\n"
            "    try:\n"
            f"        os.kill({os.getpid()}, 0)\n"
            f"        urllib.request.urlopen('{URL}/api/state', timeout=1)\n"
            f"        subprocess.run(['/usr/bin/open', '{URL}'], check=False)\n"
            "        break\n"
            "    except ProcessLookupError:\n"
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

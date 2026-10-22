#!/bin/bash
# Start runner.py detached (survives the terminal): start_runner.sh [WORKERS] [extra runner.py args ...]
cd "$(dirname "${BASH_SOURCE[0]}")"
W=${1:-8}; shift || true
PYTHONWARNINGS=ignore setsid nohup "${PYTHON:-python3}" runner.py --workers "$W" "$@" > runs_runner.out 2>&1 < /dev/null &
echo started

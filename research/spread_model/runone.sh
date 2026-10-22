#!/bin/bash
# usage: runone.sh NAME DAY REPO POLICYJSON EXECJSON FILL
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p runs/logs
"${PYTHON:-python3}" replay_sim.py --name "$1" --day "$2" --repo "$3" --policy "$4" --execution "$5" --fill "$6" > runs/logs/$1.out 2> runs/logs/$1.err
tail -1 runs/logs/$1.out

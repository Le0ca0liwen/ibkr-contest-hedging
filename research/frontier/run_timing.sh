#!/bin/bash
# Timing-only re-run (no out-of-sample builds) at raised CPU priority (nice -15), to approximate a
# dedicated core while sibling lanes keep the 4-core box at load ~11-16. Values are identical to the
# main runs (deterministic); only wall/cpu times are of interest here.
cd "$(dirname "${BASH_SOURCE[0]}")"
# App source: $REPO (default: the repository root above research/). The study used commit 1973abb; from research/:
#   git -C .. worktree add --detach ../hedge-1973abb 1973abb && REPO=$(realpath ../../hedge-1973abb) bash frontier/<this script>
export REPO=${REPO:-$(cd ../.. && pwd)}
PY=${PYTHON:-python3}
OUT=$PWD/results_timing.jsonl
for spec in "live V0" "live V2" "live V4" "live V6" "live V8" "asq V6" "asq V8"; do
  set -- $spec
  echo "START $1 $2 $(date -u +%T) load=$(cut -d' ' -f1 /proc/loadavg)" >> run_timing.log
  nice -n -15 $PY frontier_x.py --variant $2 --regime $1 --tag timing --oos-seeds "" --out $OUT >> run_timing.log 2>&1
done
echo "DONE $(date -u +%T)" >> run_timing.log

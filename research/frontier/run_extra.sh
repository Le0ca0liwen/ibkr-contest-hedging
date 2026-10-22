#!/bin/bash
# Extra variants (V6-V8) and a conservative-spread regime (asqc), run after run_all2.sh finishes.
cd "$(dirname "${BASH_SOURCE[0]}")"
until grep -q '^DONE' run2.log 2>/dev/null; do sleep 10; done
# App source: $REPO (default: the repository root above research/). The study used commit 1973abb; from research/:
#   git -C .. worktree add --detach ../hedge-1973abb 1973abb && REPO=$(realpath ../../hedge-1973abb) bash frontier/<this script>
export REPO=${REPO:-$(cd ../.. && pwd)}
PY=${PYTHON:-python3}
OUT=$PWD/results_x.jsonl
for spec in "live V6" "live V7" "live V8" "asq V6" "asq V7" "asq V8" "asqc V0" "asqc V4" "asqc V6" "asqc V7" "asqc V8"; do
  set -- $spec
  echo "START $1 $2 $(date -u +%T) load=$(cut -d' ' -f1 /proc/loadavg)" >> run_x.log
  $PY frontier_x.py --variant $2 --regime $1 --tag x --out $OUT >> run_x.log 2>&1
done
echo "DONE $(date -u +%T)" >> run_x.log

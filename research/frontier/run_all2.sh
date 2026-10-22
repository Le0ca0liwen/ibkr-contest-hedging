#!/bin/bash
# Resume run (after usage-limit interruption + container restart): full re-run, sequential, against an
# archive of commit 1973abb (the worktree HEAD has since moved), into results2.jsonl.
cd "$(dirname "${BASH_SOURCE[0]}")"
# App source: $REPO (default: the repository root above research/). The study used commit 1973abb; from research/:
#   git -C .. worktree add --detach ../hedge-1973abb 1973abb && REPO=$(realpath ../../hedge-1973abb) bash frontier/<this script>
export REPO=${REPO:-$(cd ../.. && pwd)}
PY=${PYTHON:-python3}
OUT=$PWD/results2.jsonl
for regime in live asq; do
  for v in V0 V1 V2 V3a V3b V3c V4 V5; do
    echo "START $regime $v $(date -u +%T) load=$(cut -d' ' -f1 /proc/loadavg)" >> run2.log
    $PY frontier.py --variant $v --regime $regime --tag r2 --out $OUT >> run2.log 2>&1
  done
  echo "START $regime V5-noscreen $(date -u +%T)" >> run2.log
  $PY frontier.py --variant V5 --regime $regime --noscreen --tag r2 --out $OUT >> run2.log 2>&1
done
echo "DONE $(date -u +%T)" >> run2.log

#!/bin/bash
# Sequential (one process at a time, so wall times are not contaminated by sibling runs).
cd "$(dirname "${BASH_SOURCE[0]}")"
# App source: $REPO (default: the repository root above research/). The study used commit 1973abb; from research/:
#   git -C .. worktree add --detach ../hedge-1973abb 1973abb && REPO=$(realpath ../../hedge-1973abb) bash frontier/<this script>
export REPO=${REPO:-$(cd ../.. && pwd)}
PY=${PYTHON:-python3}
for v in V0 V1 V2 V3a V3b V3c V4 V5; do
  $PY frontier.py --variant $v --regime live --tag main >> run_live.log 2>&1
done
$PY frontier.py --variant V5 --regime live --noscreen --tag main >> run_live.log 2>&1
for v in V0 V1 V2 V3a V3b V3c V4 V5; do
  $PY frontier.py --variant $v --regime asq --tag main >> run_asq.log 2>&1
done
echo DONE >> run_live.log

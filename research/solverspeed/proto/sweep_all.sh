#!/bin/bash
# One config per physical core (cores 0,2,4,...): each process runs every LP for seeds 17 and 1.
cd "$(dirname "$0")/../.."
OUT=solverspeed/out/sweep.jsonl; : > $OUT
declare -A CFG=(
 [base]='{}'
 [heur30]='{"mip_heuristic_effort":0.3}'
 [heur02]='{"mip_heuristic_effort":0.02}'
 [nosym]='{"mip_detect_symmetry":false}'
 [nopresolve]='{"presolve":"off"}'
 [thr4]='{"threads":4}'
 [lpage]='{"mip_lp_age_limit":5}'
 [minrel]='{"mip_pscost_minreliable":2}'
)
core=0
for name in "${!CFG[@]}"; do
  (for lp in n38_1000 n38_1400 n38_1540 n26_1000 n26_1400 n26_1540 n20_1540 n17_1400; do
     for seed in 17 1 2; do
       js=$(python - <<PY
import json; d=json.loads('${CFG[$name]}'); d["random_seed"]=$seed; print(json.dumps(d))
PY
)
       taskset -c $core python solverspeed/proto/sweep.py solverspeed/out/lps/$lp.npz "$name-s$seed=$js"
     done
   done >> $OUT) &
  core=$((core+2))
done
wait

#!/bin/bash
# Seed-variance study: the same primary-stage LPs with 8 HiGHS random seeds, one seed per physical core.
cd "$(dirname "$0")/../.."
OUT=solverspeed/out/seeds.txt; : > "$OUT"
core=0
for seed in 17 1 2 3 4 5 6 7; do
  (
    for lp in n38_1000 n38_1400 n38_1540 n26_1000 n26_1400 n26_1540 n20_1540; do
      taskset -c $core python solverspeed/proto/sweep.py "solverspeed/out/lps/$lp.npz" "s$seed={\"random_seed\":$seed}" >> "$OUT"
    done
  ) &
  core=$((core + 2))
done
wait
python solverspeed/proto/seed_table.py "$OUT"

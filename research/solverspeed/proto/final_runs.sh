#!/bin/bash
cd "$(dirname "$0")/../.."
source ../.venv/bin/activate
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONWARNINGS=ignore
: > solverspeed/out/final_base.jsonl; : > solverspeed/out/final_A.jsonl
echo "load at start: $(cat /proc/loadavg)" > solverspeed/out/final_load.txt
taskset -c 4 python solverspeed/proto/capacity.py base --cands 12,14,17,20,26,38 --reps 3 --out solverspeed/out/final_base.jsonl > /dev/null
taskset -c 4 python solverspeed/proto/capacity.py A --cands 12,14,17,20,26,38 --reps 3 --out solverspeed/out/final_A.jsonl > /dev/null
echo "load at end: $(cat /proc/loadavg)" >> solverspeed/out/final_load.txt
echo DONE >> solverspeed/out/final_load.txt

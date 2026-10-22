#!/bin/bash
# usage: launch.sh NAME PATH SEED VOLSCALE THRESHOLD
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p runs
"${PYTHON:-python3}" sim.py --name "$1" --start 09:30:00 --end 16:01:00 --path "$2" --seed "$3" \
  --vol-scale "$4" --threshold "$5" --frozen-clock --no-quote-files --fast-ql > "runs/$1.out" 2> "runs/$1.err"
echo "exit $?" >> "runs/$1.out"

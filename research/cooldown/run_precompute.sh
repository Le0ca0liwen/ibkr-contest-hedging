#!/bin/bash
# Precompute the per-session market tables (market/<session>.npz) with the app's ValuationEngine.
# usage: run_precompute.sh [SESSION ...]   (default: all 10 real sessions); JOBS parallel processes (default 3).
cd "$(dirname "${BASH_SOURCE[0]}")"
export PY=${PYTHON:-python3}
mkdir -p market
SESSIONS=${*:-"2026-10-08 2026-10-09 2026-10-12 2026-10-13 2026-10-14 2026-10-15 2026-10-16 2026-10-19 2026-10-20 2026-10-21"}
printf '%s\n' $SESSIONS | xargs -P "${JOBS:-3}" -I{} sh -c '"$PY" precompute.py {} > market/{}.log 2>&1'
echo ALLDONE

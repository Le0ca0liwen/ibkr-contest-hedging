#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print "The project Python environment (.venv) is missing."
  exit 1
fi
exec .venv/bin/python ops/run_contest.py

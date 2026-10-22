#!/bin/bash
# Build the spread-aware prototype app tree used by `replay_sim.py --repo proto`:
# the app source at COMMIT (default 92b4121, the study's baseline) + spread_aware_proto.patch.
# usage: make_proto.sh [COMMIT]        output: $PROTO_REPO (default: research/spread_model/proto)
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RESEARCH_ROOT=${RESEARCH_ROOT:-$(dirname "$HERE")}
REPO=${REPO:-$(dirname "$RESEARCH_ROOT")}
COMMIT=${1:-92b4121}
OUT=${PROTO_REPO:-$HERE/proto}
if [ -e "$OUT" ]; then echo "$OUT already exists; remove it first" >&2; exit 1; fi
mkdir -p "$OUT"
git -C "$REPO" archive "$COMMIT" hedge config tests | tar -x -C "$OUT"
patch -d "$OUT" -p1 < "$HERE/spread_aware_proto.patch"
echo "prototype tree: $OUT (app source $COMMIT + spread_aware_proto.patch)"

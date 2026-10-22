#!/bin/bash
# Re-pull the WSJ 1-minute (last 10 sessions) + daily bars and CBOE current-session bars, then rebuild
# every derived CSV into OUT_DIR (default: a NEW timestamped dir, so the shared data dir is not clobbered).
# Run after ~16:05 ET to get the 2026-10-21 session complete (with its official 16:00 close).
set -e
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export RESEARCH_ROOT=$(cd "${RESEARCH_ROOT:-$(dirname "$(dirname "$HERE")")}" && pwd)
PY=${PYTHON:-python3}
D=$RESEARCH_ROOT/marketdata
OUT=${1:-$D/refresh_$(date -u +%Y%m%dT%H%M)}
mkdir -p "$OUT/raw"
OUT=$(cd "$OUT" && pwd)
S=$D/scripts/wsj_fetch.sh
"$S" FUND/US/ARCX/SPY PT1M D10 "$OUT/raw/wsj_SPY_1m_D10.json"
"$S" STOCK/US/XNAS/SPCX PT1M D10 "$OUT/raw/wsj_SPCX_1m_D10.json"
"$S" STOCK/US/XNAS/RKLB PT1M D10 "$OUT/raw/wsj_RKLB_1m_D10.json"
"$S" FUND/US/ARCX/SPY P1D P1Y "$OUT/raw/wsj_SPY_1d_P1Y.json"
"$S" STOCK/US/XNAS/SPCX P1D P1Y "$OUT/raw/wsj_SPCX_1d_P1Y.json"
"$S" STOCK/US/XNAS/RKLB P1D P1Y "$OUT/raw/wsj_RKLB_1d_P1Y.json"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
for s in SPCX RKLB SPY; do curl -sSL -A "$UA" --max-time 30 -o "$OUT/raw/cboe_intraday_$s.json" "https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/$s.json" || true; done
cd "${TMPDIR:-/tmp}"
"$PY" -I "$D/scripts/build_bars.py" "$OUT/raw" "$OUT"
"$PY" -I "$D/scripts/build_options.py" "$OUT" > /dev/null
cp "$D/replay_loader.py" "$OUT/"
echo "rebuilt into $OUT"

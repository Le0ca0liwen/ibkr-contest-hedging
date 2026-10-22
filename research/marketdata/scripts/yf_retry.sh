#!/bin/bash
# Retry Yahoo chart downloads with backoff until each succeeds (HTTP 200 + JSON with timestamps).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=${1:-${RESEARCH_ROOT:-$(dirname "$(dirname "$HERE")")}/marketdata/raw}
mkdir -p "$OUT"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
JOBS="SPCX:1m:7d RKLB:1m:7d SPY:1m:7d SPCX:2m:60d RKLB:2m:60d SPY:2m:60d SPCX:5m:60d RKLB:5m:60d SPY:5m:60d"
for attempt in $(seq 1 40); do
  pending=0
  for j in $JOBS; do
    IFS=: read s iv rg <<< "$j"
    f=$OUT/yf_${s}_${iv}_${rg}.json
    if [ -s "$f" ] && grep -q '"timestamp"' "$f"; then continue; fi
    pending=1
    for h in query1 query2; do
      code=$(curl -sS -A "$UA" --max-time 60 -o $f.tmp -w "%{http_code}" "https://$h.finance.yahoo.com/v8/finance/chart/${s}?interval=${iv}&range=${rg}&includePrePost=false")
      echo "$(date -u +%H:%M:%S) attempt $attempt $h $s $iv $rg -> $code"
      if [ "$code" = "200" ] && grep -q '"timestamp"' $f.tmp; then mv $f.tmp $f; break; fi
      sleep 3
    done
  done
  [ $pending = 0 ] && { echo ALLDONE; exit 0; }
  sleep $(( 60 + attempt * 15 ))
done
echo GAVEUP

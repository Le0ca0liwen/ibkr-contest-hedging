"""Parse Nasdaq historical JSON (untrusted data) into a long CSV date,symbol,close."""
import json, sys, glob, os, csv
from datetime import datetime
src, out = sys.argv[1], sys.argv[2]
rows = []
for path in sorted(glob.glob(os.path.join(src, "hist_*.json"))):
    sym = os.path.basename(path)[5:-5]
    try:
        d = json.load(open(path))
        table = d["data"]["tradesTable"]["rows"]
    except Exception as e:
        print(sym, "unparsed", str(e)[:80]); continue
    n = 0
    for r in table:
        try:
            dt = datetime.strptime(r["date"], "%m/%d/%Y").date()
            close = float(str(r["close"]).replace("$", "").replace(",", ""))
            vol = str(r.get("volume", "")).replace(",", "")
        except Exception:
            continue
        if close > 0:
            rows.append((dt.isoformat(), sym, close, vol)); n += 1
    print(sym, n, min(x[0] for x in rows if x[1]==sym) if n else None, max(x[0] for x in rows if x[1]==sym) if n else None)
with open(out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["date", "symbol", "close", "volume"]); w.writerows(sorted(rows))

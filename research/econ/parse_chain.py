"""Parse CBOE delayed-quote option chains (untrusted JSON) into one CSV."""
import json, sys, glob, os, csv, re
src, out = sys.argv[1], sys.argv[2]
pat = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
rows = []
for path in sorted(glob.glob(os.path.join(src, "chain_*.json"))):
    d = json.load(open(path))
    data = d.get("data", {})
    under = data.get("current_price") or data.get("close")
    keys = [k for k in data.keys() if k != "options"]
    print(os.path.basename(path), d.get("timestamp"), "under", under, "bid/ask", data.get("bid"), data.get("ask"), "n", len(data.get("options", [])), "iv30", data.get("iv30"))
    for o in data.get("options", []):
        m = pat.match(o.get("option", ""))
        if not m:
            continue
        root, ymd, right, strike = m.groups()
        rows.append([os.path.basename(path)[6:-5], root, "20" + ymd, right, int(strike) / 1000, o.get("bid"), o.get("ask"), o.get("bid_size"), o.get("ask_size"), o.get("iv"), o.get("delta"), o.get("gamma"), o.get("vega"), o.get("theta"), o.get("open_interest"), o.get("volume"), under])
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "root", "expiry", "right", "strike", "bid", "ask", "bid_size", "ask_size", "iv", "delta", "gamma", "vega", "theta", "oi", "volume", "under"])
    w.writerows(rows)
print("rows", len(rows), "keys", keys)

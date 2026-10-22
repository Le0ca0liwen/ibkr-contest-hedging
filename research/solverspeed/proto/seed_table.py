import collections
import json
import re
import sys

by = collections.defaultdict(dict)
for line in open(sys.argv[1]):
    m = re.match(r"(\S+) s(\d+) (\{.*\})", line)
    if m:
        by[m[1]][int(m[2])] = json.loads(m[3])
print("%-10s %-52s | seed17 | best2 | best4 | best8   (* = not certified in 20 s)" % ("lp", "wall s by seed 17,1..7"))
for lp, d in sorted(by.items()):
    def best(seeds):
        return min((d[s]["wall"] if d[s]["ok"] else 99) for s in seeds)
    cells = " ".join("%5.1f%s" % (d[s]["wall"], "" if d[s]["ok"] else "*") for s in sorted(d))
    print("%-10s %-52s | %5.1f | %5.1f | %5.1f | %5.1f" % (lp, cells, best([17]), best([17, 1]), best([17, 1, 2, 3]), best(sorted(d))))

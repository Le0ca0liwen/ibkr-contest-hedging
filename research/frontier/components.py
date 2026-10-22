"""Per variant/regime: decompose E[score] into closing miss (excl. cost), overnight term, cost; book likewise.
Book components come from current_score only (the record keeps target components), so the book row shows the total."""
import json
import os
import sys
from collections import defaultdict

import numpy as np

LANE = os.path.join(os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontier")
files = sys.argv[1:] or [os.path.join(LANE, "results2.jsonl"), os.path.join(LANE, "results_x.jsonl")]
rows = []
for f in files:
    try:
        rows += [json.loads(l) for l in open(f) if l.strip()]
    except FileNotFoundError:
        pass
g = defaultdict(list)
for r in rows:
    g[(r["regime"], r["variant"] + ("-noscreen" if r["noscreen"] else ""))].append(r)
print(f"{'regime':<6}{'var':<12}{'n':>3} {'E[score] book':>13} {'E[score] tgt':>12} {'closing miss':>12} {'overnight(w)':>12} {'cost':>6} "
      f"{'miss+ovn (no cost)':>18} {'OOS tgt':>8} {'OOS-cost':>8} {'held options (modal)':<60}")
for (reg, v), rr in sorted(g.items(), key=lambda kv: (["live", "asq", "asqc"].index(kv[0][0]), kv[0][1])):
    comp = lambda k: np.mean([r["components"][k] for r in rr])
    closing, ovn, cost = comp("closing"), comp("overnight"), comp("scored_cost")
    from collections import Counter
    held = Counter(k for r in rr for k in r["hedges"] if k not in ("SPCX", "RKLB"))
    print(f"{reg:<6}{v:<12}{len(rr):>3} {np.mean([r['current_score'] for r in rr]):>13.0f} {np.mean([r['expected_score'] for r in rr]):>12.0f} "
          f"{closing - cost:>12.0f} {ovn:>12.0f} {cost:>6.0f} {closing - cost + ovn:>18.0f} "
          f"{np.mean([r['oos_target_score'] for r in rr]):>8.0f} {np.mean([r['oos_target_score'] - r['estimated_cost'] for r in rr]):>8.0f} "
          f"{', '.join(f'{k.replace(chr(32)+chr(50)+chr(48)+chr(50)+chr(54)+chr(45), chr(32))}x{n}' for k, n in held.most_common(5))}")

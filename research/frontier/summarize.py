"""Summarize results.jsonl: per variant/regime and per clock."""
import json
import os
import sys
from collections import defaultdict

import numpy as np

LANE = os.path.join(os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontier")
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(LANE, "results.jsonl")
rows = [json.loads(line) for line in open(path) if line.strip()]
groups = defaultdict(list)
for r in rows:
    groups[(r["regime"], r["variant"] + ("-noscreen" if r["noscreen"] else ""))].append(r)

order = ["V0", "V1", "V2", "V3a", "V3b", "V3c", "V4", "V5", "V5-noscreen"]
for regime in ("live", "asq"):
    print(f"\n=== regime {regime} ===")
    print(f"{'var':<12}{'n':>3}{'allow':>6}{'cand':>5}{'int':>5} {'OPT':>6} {'wall med/max s':>15} {'build max s':>11} "
          f"{'rebuild':>8} {'E[score] book':>13} {'E[score] tgt':>12} {'OOS book':>9} {'OOS tgt':>8} {'cost':>6} "
          f"{'stress%':>7} {'|d|max%':>7} {'gate':>5} {'elig':>5}")
    for v in order:
        g = groups.get((regime, v))
        if not g:
            continue
        ok = sum(r["status"] == "OPTIMAL" for r in g)
        walls = np.array([r["wall_s"] for r in g])
        builds = np.array([r["build_s"] for r in g])
        rebuild = np.array([r["rebuild_s"] for r in g])
        dmax = max(max(abs(x) for x in r["delta_frac"].values()) for r in g)
        print(f"{v:<12}{len(g):>3}{g[0]['allowlisted']:>6}{np.median([r['candidates'] or 0 for r in g]):>5.0f}"
              f"{np.median([r['integer_variables'] or 0 for r in g]):>5.0f} {ok:>3}/{len(g):<2} "
              f"{np.median(walls):>7.2f}/{walls.max():<6.2f} {builds.max():>11.3f} {rebuild.max():>8.3f} "
              f"{np.mean([r['current_score'] for r in g]):>13.0f} {np.mean([r['expected_score'] for r in g]):>12.0f} "
              f"{np.mean([r['oos_book_score'] for r in g]):>9.0f} {np.mean([r['oos_target_score'] for r in g]):>8.0f} "
              f"{np.mean([r['estimated_cost'] for r in g]):>6.0f} {100*max(r['stress_frac'] for r in g):>7.2f} "
              f"{100*dmax:>7.2f} {sum(r['gate_passed'] for r in g):>2}/{len(g):<2} {sum(r['trade_eligible'] for r in g):>2}/{len(g):<2}")

if "--clocks" in sys.argv:
    for (regime, v), g in sorted(groups.items()):
        print(f"\n--- {regime} {v} ---")
        for r in g:
            st = ",".join(f"{s['stage']}:{s['status']}:{s['total_ms']:.0f}ms:n{s['nodes']}" for s in r["stages"] if s.get("total_ms") is not None)
            print(f"{r['clock']} q{r['qseed']} {r['status']:<10} wall {r['wall_s']:6.2f} build {r['build_s']:.3f} "
                  f"cand {r['candidates']} book {r['current_score']:7.0f} tgt {r['expected_score']:6.0f} "
                  f"oos {r['oos_book_score']:6.0f}->{r['oos_target_score']:6.0f} cost {r['estimated_cost']:5.0f} "
                  f"stress {100*r['stress_frac']:.2f}% dSPY {100*r['delta_frac']['SPY']:.1f}% gate {r['gate_passed']} "
                  f"elig {r['trade_eligible']} [{st}] {r['hedges']} {r['gate_reasons'] or ''} "
                  f"gated={r['spread_gated']} out={r['screened_out']}")

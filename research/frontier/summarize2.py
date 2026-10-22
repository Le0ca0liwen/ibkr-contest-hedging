"""Summarize results2.jsonl: per variant/regime (+ per clock with --clocks)."""
import json
import os
import sys
from collections import defaultdict

import numpy as np

LANE = os.path.join(os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontier")
path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else os.path.join(LANE, "results2.jsonl")
rows = [json.loads(line) for line in open(path) if line.strip()]
groups = defaultdict(list)
for r in rows:
    groups[(r["regime"], r["variant"] + ("-noscreen" if r["noscreen"] else ""))].append(r)
order = ["V0", "V1", "V2", "V3a", "V3b", "V3c", "V4", "V5", "V5-noscreen", "V6", "V7", "V8"]
for regime in ("live", "asq", "asqc"):
    print(f"\n=== regime {regime} ===")
    print(f"{'var':<12}{'n':>3}{'allow':>6}{'cand':>9}{'int':>4} {'OPT':>5} {'wall med/max':>13} {'cpu med/max':>12} {'build max(cpu)':>15} "
          f"{'rebuild':>7} {'E book':>7} {'E tgt':>6} {'OOS bk':>7} {'OOS tgt':>7} {'cost':>5} {'strs%':>6} {'|dSPY|%':>7} {'|dSTK|%':>7} {'gate':>5} {'elig':>5} {'load':>5}")
    for v in order:
        g = groups.get((regime, v))
        if not g:
            continue
        ok = sum(r["status"] == "OPTIMAL" for r in g)
        walls = np.array([r["wall_s"] for r in g]); cpus = np.array([r.get("solve_cpu_s", np.nan) for r in g])
        builds = np.array([r["build_s"] for r in g]); bcpu = np.array([r.get("build_cpu_s", np.nan) for r in g])
        rebuild = np.array([r["rebuild_s"] for r in g])
        cands = [r["candidates"] or 0 for r in g]
        dspy = max(abs(r["delta_frac"]["SPY"]) for r in g)
        dstk = max(max(abs(r["delta_frac"]["SPCX"]), abs(r["delta_frac"]["RKLB"])) for r in g)
        print(f"{v:<12}{len(g):>3}{g[0]['allowlisted']:>6}{min(cands):>4}-{max(cands):<4}{int(np.median([r['integer_variables'] or 0 for r in g])):>4} {ok:>2}/{len(g):<2} "
              f"{np.median(walls):>6.2f}/{walls.max():<6.2f} {np.median(cpus):>5.2f}/{cpus.max():<6.2f} {builds.max():>7.3f}({bcpu.max():.3f}) {rebuild.max():>7.3f} "
              f"{np.mean([r['current_score'] for r in g]):>7.0f} {np.mean([r['expected_score'] for r in g]):>6.0f} "
              f"{np.mean([r['oos_book_score'] for r in g]):>7.0f} {np.mean([r['oos_target_score'] for r in g]):>7.0f} "
              f"{np.mean([r['estimated_cost'] for r in g]):>5.0f} {100*max(r['stress_frac'] for r in g):>6.2f} "
              f"{100*dspy:>7.2f} {100*dstk:>7.2f} {sum(r['gate_passed'] for r in g):>2}/{len(g):<2} {sum(r['trade_eligible'] for r in g):>2}/{len(g):<2} {np.mean([r['load1'] for r in g]):>5.1f}")
    # per clock means for value
    print(f"\n  per-clock mean E[score] target (book) and max wall:")
    for v in order:
        g = groups.get((regime, v))
        if not g:
            continue
        cells = []
        for clk in ("1000", "1230", "1400", "1500", "1540"):
            gg = [r for r in g if r["clock"] == clk]
            if gg:
                cells.append(f"{clk}: {np.mean([r['expected_score'] for r in gg]):5.0f} ({np.mean([r['current_score'] for r in gg]):5.0f}) "
                             f"w{max(r['wall_s'] for r in gg):4.2f} c{max(r.get('solve_cpu_s', np.nan) for r in gg):4.2f}")
        print(f"  {v:<12} " + " | ".join(cells))

if "--clocks" in sys.argv:
    for (regime, v), g in sorted(groups.items()):
        print(f"\n--- {regime} {v} ---")
        for r in g:
            st = ",".join(f"{s['stage']}:{s['status']}:{s['total_ms']:.0f}ms:n{s['nodes']}" for s in r["stages"] if s.get("total_ms") is not None)
            print(f"{r['clock']} q{r['qseed']} {r['status']:<10} wall {r['wall_s']:5.2f} cpu {r.get('solve_cpu_s', float('nan')):5.2f} build {r['build_s']:.3f} "
                  f"cand {r['candidates']} book {r['current_score']:6.0f} tgt {r['expected_score']:5.0f} "
                  f"oos {r['oos_book_score']:5.0f}->{r['oos_target_score']:5.0f} cost {r['estimated_cost']:4.0f} "
                  f"stress {100*r['stress_frac']:.2f}% dSPY {100*r['delta_frac']['SPY']:.1f}% gate {r['gate_passed']} "
                  f"[{st}] {r['hedges']} {r['gate_reasons'] or ''} gated={r['spread_gated']} out={r['screened_out']}")

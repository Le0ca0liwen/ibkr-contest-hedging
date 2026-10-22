"""Aggregate harness.py JSONL output; compare tags (sources) side by side.

    python summarize.py results.jsonl [more.jsonl ...] [--tags base,variant] [--by-clock]

Prints, per candidate count (and clock with --by-clock), median [min-max] seconds of:
  solve total, construct, CVXPY canonicalization (stage-1 get_problem_data), HiGHS run per stage,
  other CVXPY per-solve overhead, post, warm/cold scenario build; plus status counts and the
  returned holdings' agreement across tags (same hedge = same integer quantities).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict


def fmt(values):
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    med = statistics.median(values)
    return f"{med:.3f}" if len(values) == 1 else f"{med:.3f} [{min(values):.3f}-{max(values):.3f}]"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--tags")
    p.add_argument("--by-clock", action="store_true")
    args = p.parse_args()
    rows = []
    for f in args.files:
        with open(f) as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    tags = args.tags.split(",") if args.tags else sorted({r["tag"] for r in rows})
    rows = [r for r in rows if r["tag"] in tags]

    def key(r):
        return (r["n"], r["clock"]) if args.by_clock else (r["n"],)

    solves = defaultdict(lambda: defaultdict(list))
    builds = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["kind"] == "solve" and r["mode"] == "full":
            solves[key(r)][r["tag"]].append(r)
        elif r["kind"] == "build":
            builds[key(r)][r["tag"]].append(r)
    print("SOLVE (full mode) seconds: median [min-max]")
    for k in sorted(solves):
        for tag in tags:
            rs = solves[k].get(tag, [])
            if not rs:
                continue
            statuses = defaultdict(int)
            for r in rs:
                statuses[r["status"]] += 1
            stage_names = []
            for r in rs:
                for s in r["stages"]:
                    if s["stage"] not in stage_names:
                        stage_names.append(s["stage"])
            canon = [r["stages"][0]["canon_s"] if r["stages"] else None for r in rs]
            per_stage = []
            for name in stage_names:
                runs = [next((s["run_s"] for s in r["stages"] if s["stage"] == name), None) for r in rs]
                per_stage.append(f"{name}={fmt(runs)}")
            other = []
            for r in rs:
                cvx = sum(s["solve_gpd_s"] + s["unpack_s"] + (s.get("highs_overhead_s") or 0) + s["passModel_s"]
                          + (s["canon_s"] if i else 0) for i, s in enumerate(r["stages"]))
                other.append(cvx)
            print(f"n={k} {tag:<12} {dict(statuses)} total={fmt([r['total_s'] for r in rs])} "
                  f"construct={fmt([r['construct_s'] + r['metrics_s'] for r in rs])} canon={fmt(canon)} "
                  f"highs: {' '.join(per_stage)} cvxpy_other={fmt(other)} post={fmt([r['post_s'] for r in rs])}")
        if len(tags) > 1:
            hold = {tag: {json.dumps(r["holdings"], sort_keys=True) for r in solves[k].get(tag, []) if r["status"] == "OPTIMAL"}
                    for tag in tags}
            base = hold.get(tags[0], set())
            for tag in tags[1:]:
                same = hold.get(tag) == base
                print(f"      holdings {tags[0]} vs {tag}: {'IDENTICAL' if same else 'DIFFER'} "
                      f"({len(base)} vs {len(hold.get(tag, set()))} distinct optimal results)")
    print("\nSCENARIO BUILD seconds: median [min-max] (warm = engine caches filled)")
    for k in sorted(builds):
        for tag in tags:
            rs = builds[k].get(tag, [])
            if not rs:
                continue
            warm = [r for r in rs if not r["cold"]]
            cold = [r for r in rs if r["cold"]]
            ph = defaultdict(list)
            for r in warm or rs:
                for name, v in r["phases"].items():
                    ph[name].append(v)
            parts = " ".join(f"{n}={fmt(v)}" for n, v in ph.items() if statistics.median(v) >= 0.005)
            print(f"n={k} {tag:<12} cold={fmt([r['build_s'] for r in cold])} warm={fmt([r['build_s'] for r in warm])} "
                  f"price_calls={rs[0]['price_calls']} price_s={fmt([r['price_s'] for r in warm or rs])} | {parts}")


if __name__ == "__main__":
    main()

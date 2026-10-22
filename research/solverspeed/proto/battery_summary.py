import json, sys, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
print("instances", len(rows))
print("lp identical (all stages):", sum(r["lp_identical"] for r in rows), "/", len(rows))
print("status pairs:", collections.Counter(tuple(r["status"]) for r in rows))
print("same quantities:", sum(r["same_quantities"] for r in rows), "/", len(rows))
bad = [r for r in rows if not r["same_quantities"]]
def close(a, b, t=1e-6):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= t * max(1, abs(a)))
print("primary incumbent equal (1e-6 rel):", sum(close(r["primary_base"], r["primary_proto"]) for r in rows))
print("primary bound equal (1e-6 rel):", sum(close(r["bound_base"], r["bound_proto"]) for r in rows))
print("score equal:", sum(close(r["score"][0], r["score"][1]) for r in rows))
print("ranking_complete pairs:", collections.Counter(tuple(r["ranking"]) for r in rows))
print("candidate counts:", collections.Counter(r["candidates"] for r in rows), "stock_only:", sum(r["spec"]["stock_only"] for r in rows), "hedged:", sum(r["spec"]["hedged"] for r in rows))
for r in bad:
    print("DIFF seed", r["spec"]["seed"], r["spec"]["n"], r["spec"]["clock"], r["status"], r["ranking"], "score", r["score"], "stages", r["stages_base"][-1][:2], r["stages_proto"][-1][:2])
tb = sorted(r["total_ms"][0] for r in rows); tp = sorted(r["total_ms"][1] for r in rows)
print("total ms median base/proto (12 parallel workers, indicative only):", tb[len(tb)//2], tp[len(tp)//2])

"""Plan execution ratio: planned vs executed contracts/shares per eligible decision; slice-gate reasons."""
import json, sys
from collections import Counter, defaultdict
from analyze import summarize, load, OPTION_IDS
def planexec(run):
    rows, rep = load(run)
    s = summarize(run)
    submits = {r["order_id"]: r for r in rows if r["ev"] == "submit"}
    by_dec = defaultdict(lambda: [0, 0])
    for f in rep["fills"]:
        d = submits.get(f["order_id"], {}).get("decision_id")
        by_dec[d][0 if f["kind"] == "OPT" else 1] += abs(f["qty"])
    ids = dict(zip(rep["decision_ids"], rep["decisions"]))
    planned_opt = planned_stk = done_opt = done_stk = 0
    for e in s["eligible_rows"]:
        planned_opt += sum(abs(v) for v in e["opt_change"].values())
        planned_stk += sum(abs(v) for v in e["stk_change"].values())
    done_opt = sum(v[0] for v in by_dec.values()); done_stk = sum(v[1] for v in by_dec.values())
    econ = [r for r in rows if r["ev"] == "econ"]
    rej = Counter()
    for r in econ:
        if not r["ok"]:
            rej["neg_exposure" if r["exposure"] <= 0.01 else ("net<=1.645SE" if r["net"] > 0 else "net<=0")] += 1
    slices = Counter(f"{r['submitted']}|{r['reason'][:60]}" for r in rows if r["ev"] == "slice")
    return dict(name=s["name"], eligible=s["eligible"], planned_opt=planned_opt, executed_opt=done_opt,
                planned_stk=planned_stk, executed_stk=done_stk, econ_checks=len(econ), econ_pass=sum(r["ok"] for r in econ),
                econ_reject=dict(rej), slices=dict(slices), hardstop=any("Working-order check" in r.get("health", "") for r in rows if r["ev"] == "tick"))
for run in sys.argv[1:]:
    print(json.dumps(planexec(run)))

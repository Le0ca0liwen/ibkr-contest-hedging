"""Aggregate cooldown-lane runs: python analyze.py [runs_dir] > report.txt"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
RUNS = Path(sys.argv[1] if len(sys.argv) > 1 else
            Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve() / "cooldown" / "runs")


def reversals(fills, kind="OPT"):
    by = defaultdict(list)
    for f in fills:
        if f["kind"] == kind:
            by[f["con_id"]].append(f["qty"])
    flips, round_trip = 0, 0
    for seq in by.values():
        signs = [1 if q > 0 else -1 for q in seq]
        flips += sum(a != b for a, b in zip(signs, signs[1:], strict=False))
        bought = sum(q for q in seq if q > 0)
        sold = -sum(q for q in seq if q < 0)
        round_trip += min(bought, sold)
    return flips, round_trip


def solve_statuses(run, stock_only=None):
    out = defaultdict(int)
    for line in open(run / "log.jsonl"):
        if '"kind": "solve"' in line:
            r = json.loads(line)
            if stock_only is None or r["stock_only"] == stock_only:
                out[r["status"]] += 1
    return out


def load():
    rows = []
    for s in sorted(RUNS.glob("*/summary.json")):
        x = json.loads(s.read_text())
        name = s.parent.name
        if name.startswith("warm_"):
            continue
        variant = name.rsplit("__", 1)[0]
        fills = x["fills"]
        opt = [f for f in fills if f["kind"] == "OPT"]
        stk = [f for f in fills if f["kind"] == "STK"]
        flips, rt = reversals(fills)
        spread = sum(f["spread_cost"] for f in fills)
        fees = sum(f["fee"] for f in fills)
        rows.append({
            "variant": variant, "session": x["session"], "miss": x["miss"], "abs_miss": abs(x["miss"]),
            "miss_ex_cost": x["miss"] + spread + fees, "spread": spread, "fees": fees,
            "spread_opt": sum(f["spread_cost"] for f in opt), "spread_stk": sum(f["spread_cost"] for f in stk),
            "fills": len(fills), "opt_fills": len(opt), "stk_fills": len(stk),
            "contracts": sum(abs(f["qty"]) for f in opt), "shares": sum(abs(f["qty"]) for f in stk),
            "flips": flips, "round_trip": rt, "solves": x["n_solves"], "full": x["n_full_solves"],
            "eligible": sum(bool(d.get("trade_eligible")) for d in x["decisions"]),
            "mode": x["mode"], "first_paper": x["first_paper"], "hedges": x["hedges"], "wall": x["wall_s"],
            "stops": len((x.get("operator") or {}).get("stops", [])),
            "paused_min": (x.get("operator") or {}).get("paused_seconds", 0) / 60,
            "corrective": sum(bool(d.get("corrective")) for d in x["decisions"]),
            "corrective_stock": sum(d.get("optimization_scope") == "CORRECTIVE_STOCK" for d in x["decisions"]),
            "timeouts": solve_statuses(s.parent).get("USER_LIMIT", 0),
            "infeasible_stock": solve_statuses(s.parent, stock_only=True).get("INFEASIBLE", 0),
        })
    return rows


def main():
    rows = load()
    by = defaultdict(list)
    for r in rows:
        by[r["variant"]].append(r)
    sessions = sorted({r["session"] for r in rows})
    print("runs", len(rows), "variants", len(by), "sessions", sessions)
    cols = ["n", "|miss| med", "|miss| mean", "|miss| sd", "miss mean", "|exc| mean", "spread/d", "fees/d",
            "|miss|+cost", "fills/d", "opt fills", "ctrs/d", "shares/d", "flips/d", "rt ctr/d", "full/d",
            "stops/d", "paused min", "corr dec/d", "corrSTK/d", "timeouts", "infeasSTK"]
    print("\t".join(["variant", *cols]))
    table = {}
    for v, rs in sorted(by.items()):
        a = lambda k: np.array([r[k] for r in rs], dtype=float)  # noqa: E731
        absm = a("abs_miss")
        cost = a("spread") + a("fees")
        line = [len(rs), np.median(absm), absm.mean(), absm.std(ddof=1) if len(rs) > 1 else 0, a("miss").mean(),
                np.abs(a("miss_ex_cost")).mean(), a("spread").mean(), a("fees").mean(),
                (np.abs(a("miss_ex_cost")) + cost).mean(), a("fills").mean(), a("opt_fills").mean(),
                a("contracts").mean(), a("shares").mean(), a("flips").mean(), a("round_trip").mean(), a("full").mean(),
                a("stops").mean(), a("paused_min").mean(), a("corrective").mean(), a("corrective_stock").mean(),
                a("timeouts").mean(), a("infeasible_stock").mean()]
        table[v] = line
        print("\t".join([v, *[f"{x:.1f}" if isinstance(x, float) else str(x) for x in line]]))
    print("\nper-session |miss| (rows: variant; cols: sessions)")
    print("\t".join(["variant", *[s[5:] for s in sessions]]))
    for v, rs in sorted(by.items()):
        d = {r["session"]: r for r in rs}
        print("\t".join([v, *[f"{d[s]['miss']:+.0f}" if s in d else "-" for s in sessions]]))
    print("\nper-session costs (spread+fees)")
    for v, rs in sorted(by.items()):
        d = {r["session"]: r for r in rs}
        print("\t".join([v, *[f"{d[s]['spread'] + d[s]['fees']:.0f}" if s in d else "-" for s in sessions]]))
    print("\nper-session option contracts traded / flips")
    for v, rs in sorted(by.items()):
        d = {r["session"]: r for r in rs}
        print("\t".join([v, *[f"{d[s]['contracts']}/{d[s]['flips']}" if s in d else "-" for s in sessions]]))
    # paired comparison vs baseline per fill model
    print("\npaired differences vs the 900 s / $100 baseline of the same fill model (|miss| and cost)")
    for v, rs in sorted(by.items()):
        fill = v.split("_")[0]
        base = f"{fill}_o900_t100"
        if v == base or base not in by:
            continue
        b = {r["session"]: r for r in by[base]}
        diff = [r["abs_miss"] - b[r["session"]]["abs_miss"] for r in rs if r["session"] in b]
        dc = [(r["spread"] + r["fees"]) - (b[r["session"]]["spread"] + b[r["session"]]["fees"]) for r in rs
              if r["session"] in b]
        if not diff:
            continue
        diff, dc = np.array(diff), np.array(dc)
        se = diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else 0
        print(f"{v}\tn={len(diff)}\td|miss| mean {diff.mean():+.1f} (SE {se:.1f}) median {np.median(diff):+.1f} "
              f"better in {int((diff < 0).sum())}/{len(diff)}\tdcost mean {dc.mean():+.1f}")
    ref_path = RUNS / "reference.json"
    if ref_path.exists():
        ref = json.loads(ref_path.read_text())
        print("\nno-trade references on the same sessions as each variant (|miss| mean / median)")
        for v, rs in sorted(by.items()):
            ss = [r["session"] for r in rs if r["session"] in ref]
            hold = np.array([abs(ref[s]["hold_0945"]) for s in ss])
            none = np.array([abs(ref[s]["none"]) for s in ss])
            mine = np.array([r["abs_miss"] for r in rs if r["session"] in ref])
            if len(ss):
                print(f"{v}\tn={len(ss)}\tvariant {mine.mean():.0f}/{np.median(mine):.0f}\thold09:45 "
                      f"{hold.mean():.0f}/{np.median(hold):.0f}\tnever-hedged {none.mean():.0f}/{np.median(none):.0f}")
        print("per-session references:", {s: (round(r["none"]), round(r["hold_0945"])) for s, r in sorted(ref.items())})
    # balanced view: sessions where every core variant of the fill model finished
    for fill in sorted({v.split("_")[0] for v in by}):
        for t in (100, 50):
            core = [f"{fill}_o{o}_t{t}" for o in (60, 120, 300, 900)]
            if not all(c in by for c in core):
                continue
            common = set.intersection(*({r["session"] for r in by[c]} for c in core))
            if not common:
                continue
            print(f"\nbalanced {fill} t{t} on {len(common)} sessions {sorted(common)}")
            for c in core:
                rs = [r for r in by[c] if r["session"] in common]
                absm = np.array([r["abs_miss"] for r in rs])
                cost = np.array([r["spread"] + r["fees"] for r in rs])
                exc = np.array([abs(r["miss_ex_cost"]) for r in rs])
                print(f"  {c}\t|miss| mean {absm.mean():.0f} median {np.median(absm):.0f} sd {absm.std(ddof=1) if len(rs) > 1 else 0:.0f}"
                      f"\tcost mean {cost.mean():.0f}\t|miss ex cost|+cost {np.mean(exc + cost):.0f}"
                      f"\tctrs {np.mean([r['contracts'] for r in rs]):.1f} shares {np.mean([r['shares'] for r in rs]):.0f}"
                      f" flips {np.mean([r['flips'] for r in rs]):.1f} stops {np.mean([r['stops'] for r in rs]):.1f}")
    json.dump(rows, open(RUNS / "rows.json", "w"), default=str, indent=0)


if __name__ == "__main__":
    main()

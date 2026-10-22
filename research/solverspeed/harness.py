"""Per-phase timing harness for ScenarioEngine.build + HedgeOptimizer.solve (no broker, no network).

    HEDGE_SRC=<source dir> python solverspeed/harness.py \
        [--cands 12,14,17,20,26,38] [--clocks 10:00,14:00,15:40] [--reps 3] [--stock-only 12,38] \
        [--no-highs-log] [--sizes] [--dump-mps DIR] [--out results.jsonl] [--tag name]

HEDGE_SRC defaults to $REPO, else the repository root above research/ (the study profiled commit 1973abb).
Inputs are identical for every source: cached quotes (cases/quotes_*.json), the real Oct 20 daily
history (SPCX/RKLB/SPY), the small.contest.yaml manifest with the live template policy
(burn_surplus false, overnight_weight 0.5, primary_band 20, solver_seconds 10).

Repetitions are interleaved (rep 1 of every case, then rep 2, ...). One ScenarioEngine persists per
case (like the app), so build #1 is cold (covariance/beta caches empty) and later builds are warm.

Per solve it splits wall time into:
  construct     solve() entry -> cp.Problem object exists (filters, RiskEngine.evaluate, expressions)
     risk_eval / stress_proj   sub-parts of construct
  metrics       Problem exists -> first stage starts (problem.variables(), size_metrics)
  per stage     canon (stage()'s explicit get_problem_data; stage 1 = full CVXPY canonicalization,
                later = DPP parameter re-application), solve_gpd (get_problem_data again inside
                problem.solve, cached), highs_prep (CVXPY->HighsModel arrays), passModel, run (HiGHS),
                highs_post (getSolution/getBasis/getInfo), unpack (CVXPY unpack_results), and HiGHS
                log numbers (presolve time, presolved size, nodes, LP iterations, gap, status)
  post          last problem.solve return -> solve() return (execution_cost, _score, Target)
and per build: refresh_daily_history, trading_variance, covariance, changes/next/overnight
_reprice, stress_matrix, market_betas, rhos, inline (market stress, normals), digest, plus the
number and total time of ValuationEngine.price calls attributed to each phase.

Prints one JSON line per measurement (and appends to --out) plus a summary table (medians, min-max).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from time import perf_counter

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
SRC = os.environ.get("HEDGE_SRC") or os.environ.get("REPO") or str(RESEARCH_ROOT.parent)
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cvxpy as cp  # noqa: E402
import highspy  # noqa: E402
import numpy as np  # noqa: E402
from cvxpy.reductions.solvers.conic_solvers import highs_conif  # noqa: E402
from cvxpy.reductions.solvers.solving_chain import SolvingChain  # noqa: E402

import cases  # noqa: E402
from hedge import optimizer as optmod  # noqa: E402
from hedge import risk as riskmod  # noqa: E402
from hedge import scenarios as scenmod  # noqa: E402
from hedge import valuation as valmod  # noqa: E402

LANE = RESEARCH_ROOT / "solverspeed"
LOGDIR = LANE / "highs_logs"

# --------------------------------------------------------------------------------------------
# Instrumentation (wrappers only; the code under test is unchanged)
# --------------------------------------------------------------------------------------------
STATE = {"solve": None, "build": None, "highs_log": True, "dump_mps": None, "case": "", "stage_no": 0}


def _wrap(owner, name, before=None, after=None):
    original = getattr(owner, name)

    def wrapper(*args, **kwargs):
        token = before(*args, **kwargs) if before else None
        t0 = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            if after:
                after(token, t0, perf_counter(), args, kwargs)

    wrapper.__wrapped__ = original
    setattr(owner, name, wrapper)
    return original


# ---- optimizer side ------------------------------------------------------------------------
LAST = {}


def install():
    # HedgeOptimizer.solve: total + record
    original_solve = optmod.HedgeOptimizer.solve

    def solve(self, *args, **kwargs):
        rec = {"t0": perf_counter(), "problem_at": None, "risk_eval": 0.0, "stress_proj": 0.0, "stages": [],
               "in_solve": False, "last_solve_end": None, "model": None, "problem": None}
        STATE["solve"] = rec
        try:
            return original_solve(self, *args, **kwargs)
        finally:
            rec["t1"] = perf_counter()
            STATE["solve"] = None
            LAST["solve"] = rec

    optmod.HedgeOptimizer.solve = solve

    # cp.Problem.__init__: first problem created inside solve = the hedge model
    original_init = cp.Problem.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        rec = STATE["solve"]
        if rec is not None and rec["problem_at"] is None:
            rec["problem_at"] = perf_counter()
            rec["problem"] = self

    cp.Problem.__init__ = init

    def add(key):
        def after(token, t0, t1, args, kwargs):
            rec = STATE["solve"]
            if rec is not None:
                rec[key] += t1 - t0
        return after

    _wrap(riskmod.RiskEngine, "evaluate", after=add("risk_eval"))
    _wrap(optmod, "_stress_projection", after=add("stress_proj"))

    # get_problem_data: explicit (stage start) vs inside problem.solve
    def gpd_after(token, t0, t1, args, kwargs):
        rec = STATE["solve"]
        if rec is None:
            return
        if rec["in_solve"]:
            rec["stages"][-1]["solve_gpd"] = t1 - t0
        else:
            rec["stages"].append({"t_start": t0, "canon": t1 - t0, "first_canon": not rec["stages"]})

    _wrap(cp.Problem, "get_problem_data", after=gpd_after)

    def psolve_before(self, *a, **k):
        rec = STATE["solve"]
        if rec is not None:
            rec["in_solve"] = True

    def psolve_after(token, t0, t1, args, kwargs):
        rec = STATE["solve"]
        if rec is not None:
            rec["in_solve"] = False
            rec["stages"][-1]["psolve"] = t1 - t0
            rec["stages"][-1]["t_end"] = t1
            rec["last_solve_end"] = t1

    _wrap(cp.Problem, "solve", before=psolve_before, after=psolve_after)

    def stage_set(key):
        def after(token, t0, t1, args, kwargs):
            rec = STATE["solve"]
            if rec is not None and rec["stages"]:
                rec["stages"][-1][key] = rec["stages"][-1].get(key, 0) + t1 - t0
        return after

    _wrap(SolvingChain, "solve_via_data", after=stage_set("chain_svd"))
    _wrap(highs_conif.HIGHS, "solve_via_data", after=stage_set("highs_svd"))
    _wrap(cp.Problem, "unpack_results", after=stage_set("unpack"))

    original_pass = highspy.Highs.passModel

    def pass_model(self, model, *a, **k):
        t0 = perf_counter()
        r = original_pass(self, model, *a, **k)
        t1 = perf_counter()
        rec = STATE["solve"]
        if rec is not None and rec["stages"]:
            st = rec["stages"][-1]
            st["passModel"] = t1 - t0
            lp = model.lp_
            ints = sum(1 for v in lp.integrality_ if v == highspy.HighsVarType.kInteger) if len(lp.integrality_) else 0
            st["model"] = {"rows": int(lp.num_row_), "cols": int(lp.num_col_),
                           "nnz": int(len(lp.a_matrix_.value_)), "int_cols": ints}
            if STATE["dump_mps"]:
                Path(STATE["dump_mps"]).mkdir(parents=True, exist_ok=True)
                self.writeModel(str(Path(STATE["dump_mps"]) / f"{STATE['case']}_stage{len(rec['stages'])}.mps"))
        return r

    highspy.Highs.passModel = pass_model

    original_set_solution = highspy.Highs.setSolution

    def set_solution(self, *a, **k):
        rec = STATE["solve"]
        if rec is not None and rec["stages"]:
            rec["stages"][-1]["warm_start"] = True
            if STATE["dump_mps"] and a and hasattr(a[0], "col_value"):
                np.save(Path(STATE["dump_mps"]) / f"{STATE['case']}_stage{len(rec['stages'])}.start.npy",
                        np.asarray(a[0].col_value))
        return original_set_solution(self, *a, **k)

    highspy.Highs.setSolution = set_solution

    original_run = highspy.Highs.run

    def run(self, *a, **k):
        rec = STATE["solve"]
        log = None
        if rec is not None and STATE["highs_log"]:
            LOGDIR.mkdir(parents=True, exist_ok=True)
            STATE["stage_no"] += 1
            log = LOGDIR / f"{STATE['case']}_{STATE['stage_no']}.log"
            self.setOptionValue("log_file", str(log))
        t0 = perf_counter()
        r = original_run(self, *a, **k)
        t1 = perf_counter()
        if rec is not None and rec["stages"]:
            st = rec["stages"][-1]
            st["run"] = t1 - t0
            st["run_t0"], st["run_t1"] = t0, t1
            info = self.getInfo()
            st["highs"] = {
                "run_time": self.getRunTime(),
                "status": self.getModelStatus().name,
                "nodes": int(info.mip_node_count),
                "mip_gap": float(info.mip_gap) if np.isfinite(info.mip_gap) else None,
                "dual_bound": float(info.mip_dual_bound) if np.isfinite(info.mip_dual_bound) else None,
                "objective": float(info.objective_function_value),
                "simplex_iters": int(info.simplex_iteration_count),
            }
            if log is not None:
                self.setOptionValue("log_file", "")
                st["highs"].update(parse_highs_log(log))
        return r

    highspy.Highs.run = run

    # ---- scenario side --------------------------------------------------------------------
    original_build = scenmod.ScenarioEngine.build

    def build(self, *args, **kwargs):
        rec = {"t0": perf_counter(), "phases": defaultdict(float), "price": defaultdict(lambda: [0, 0.0]),
               "stack": ["inline"], "reprice_no": 0}
        STATE["build"] = rec
        try:
            return original_build(self, *args, **kwargs)
        finally:
            rec["t1"] = perf_counter()
            STATE["build"] = None
            LAST["build"] = rec

    scenmod.ScenarioEngine.build = build

    def phase(label_fn):
        def before(*args, **kwargs):
            rec = STATE["build"]
            if rec is None:
                return None
            label = label_fn(rec)
            rec["stack"].append(label)
            return label

        def after(label, t0, t1, args, kwargs):
            rec = STATE["build"]
            if rec is None or label is None:
                return
            rec["stack"].pop()
            if label not in rec["stack"]:  # count outermost only
                rec["phases"][label] += t1 - t0
        return before, after

    def reprice_label(rec):
        rec["reprice_no"] += 1
        return ["reprice_changes", "reprice_next", "reprice_overnight"][min(rec["reprice_no"], 3) - 1]

    for owner, name, lab in [
        (scenmod.ScenarioEngine, "refresh_daily_history", lambda r: "refresh_daily"),
        (scenmod.ScenarioEngine, "daily_covariance", lambda r: "covariance"),
        (scenmod.ScenarioEngine, "market_betas", lambda r: "market_betas"),
        (scenmod.ScenarioEngine, "stress_matrix", lambda r: "stress_matrix"),
        (scenmod.ScenarioEngine, "_reprice", reprice_label),
        (scenmod, "trading_variance", lambda r: "trading_variance"),
        (scenmod, "digest", lambda r: "digest"),
        (valmod.ValuationEngine, "rho", lambda r: "rhos"),
        (scenmod.ScenarioSet, "__post_init__", lambda r: "scenarioset_validate"),
    ]:
        b, a = phase(lab)
        _wrap(owner, name, before=b, after=a)

    original_price = valmod.ValuationEngine.price

    def price(self, *args, **kwargs):
        rec = STATE["build"]
        if rec is None:
            return original_price(self, *args, **kwargs)
        t0 = perf_counter()
        try:
            return original_price(self, *args, **kwargs)
        finally:
            slot = rec["price"][rec["stack"][-1]]
            slot[0] += 1
            slot[1] += perf_counter() - t0

    valmod.ValuationEngine.price = price


_NUM = r"([-+0-9.eE]+|inf)"


def parse_highs_log(path):
    text = Path(path).read_text(errors="replace")
    out = {}
    m = re.search(r"MIP has (\d+) rows; (\d+) cols; (\d+) nonzeros; (\d+) integer variables \((\d+) binary\)", text)
    if m:
        out["orig"] = dict(zip(("rows", "cols", "nnz", "ints", "binary"), map(int, m.groups()), strict=True))
    m = re.search(r"Presolve reductions: rows (\d+)\(-?(\d+)\); columns (\d+)\(-?(\d+)\); nonzeros (\d+)\(-?(\d+)\)", text)
    if m:
        g = list(map(int, m.groups()))
        out["presolved"] = {"rows": g[0], "cols": g[2], "nnz": g[4]}
    if "Presolve reductions" not in text and re.search(r"Presolve: (Optimal|Infeasible)|reduced to empty", text):
        out["presolved"] = "empty"
    m = re.search(r"Solving MIP model with:\s+(\d+) rows\s+(\d+) cols \((\d+) binary, (\d+) integer", text)
    if m:
        out["mip_model"] = dict(zip(("rows", "cols", "binary", "integer"), map(int, m.groups()), strict=True))
    m = re.search(r"Timing\s+" + _NUM + r"\s*(?:\(total\))?\s+" + _NUM + r" \((?:P|p)resolve\)", text)
    if m:
        out["t_total"], out["t_presolve"] = float(m.group(1)), float(m.group(2))
    m = re.search(_NUM + r" \((?:S|s)olve\)", text)
    if m:
        out["t_solve"] = float(m.group(1))
    m = re.search(r"\(Presolve\)\s+MIP\s+time \[calls\] = " + _NUM + r" \[\d+\]\s+subMIP time \[calls\] = " + _NUM + r" \[(\d+)\]", text)
    if m:
        out["t_presolve_mip"], out["t_presolve_submip"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"\(Solve\)\s+MIP\s+time \[calls\] = " + _NUM + r" \[\d+\]\s+subMIP time \[calls\] = " + _NUM + r" \[(\d+)\]", text)
    if m:
        out["t_solve_mip"], out["t_solve_submip"], out["submip_calls"] = float(m.group(1)), float(m.group(2)), int(m.group(3))
    report = text[text.find("Solving report"):] if "Solving report" in text else ""
    m = re.search(r"Status\s+(.+)", report)
    if m:
        out["status_text"] = m.group(1).strip()
    m = re.search(r"\n\s+Gap\s+(\S+)", report)
    if m:
        out["gap_text"] = m.group(1)
    m = re.search(r"Nodes\s+(\d+)", report)
    if m:
        out["nodes_report"] = int(m.group(1))
    out["bb_lines"] = sum(1 for line in text.splitlines() if re.match(r"^\s[A-Za-z ]\s+\d+\s+\d+\s+\d+\s+[\d.]+%", line))
    m = re.search(r"LP iterations\s+(\d+)\s+(\d+) \(strong br\.\)\s+(\d+) \(separation\)\s+(\d+) \(heuristics\)", text)
    if m:
        out["lp_iters"], out["lp_iters_strong"], out["lp_iters_sep"], out["lp_iters_heur"] = map(int, m.groups())
    m = re.search(r"Max sub-MIP depth\s+(\d+)", text)
    if m:
        out["submip_depth"] = int(m.group(1))
    # time (s) at which the final incumbent was found: last B&B line with a source letter
    best = None
    for line in text.splitlines():
        mm = re.match(r"^\s([A-Za-z])\s+\d+\s+\d+\s+\d+\s+[\d.]+%\s+\S+\s+(\S+)\s+\S+.*\s([\d.]+)s\s*$", line)
        if mm:
            best = (mm.group(1), float(mm.group(3)))
    if best:
        out["last_incumbent_src"], out["last_incumbent_t"] = best
    return out


# --------------------------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------------------------
def summarize_solve(rec, target):
    t0, t1 = rec["t0"], rec["t1"]
    problem_at = rec["problem_at"] or t1
    stages_out = []
    names = [s["stage"] for s in target.solver_metrics.get("stages", [])]
    for k, st in enumerate(rec["stages"]):
        highs_svd = st.get("highs_svd", 0.0)
        run = st.get("run", 0.0)
        pm = st.get("passModel", 0.0)
        o = {
            "stage": names[k] if k < len(names) else f"#{k}",
            "canon_s": round(st.get("canon", 0.0), 4),
            "solve_gpd_s": round(st.get("solve_gpd", 0.0), 4),
            "highs_prep_s": None,
            "passModel_s": round(pm, 4),
            "run_s": round(run, 4),
            "unpack_s": round(st.get("unpack", 0.0), 4),
            "stage_total_s": round(st.get("t_end", st["t_start"]) - st["t_start"], 4),
            "model": st.get("model"),
            "warm_start": st.get("warm_start", False),
            "highs": st.get("highs"),
        }
        if "run_t0" in st:
            # highs_svd = prep + passModel + run + post (getSolution/getBasis/getInfo/ray)
            o["highs_overhead_s"] = round(highs_svd - run - pm, 4)
        stages_out.append(o)
    first_stage = rec["stages"][0]["t_start"] if rec["stages"] else t1
    last_end = rec["last_solve_end"] or first_stage
    sm = target.solver_metrics
    return {
        "status": target.status,
        "total_s": round(t1 - t0, 4),
        "construct_s": round(problem_at - t0, 4),
        "risk_eval_s": round(rec["risk_eval"], 4),
        "stress_proj_s": round(rec["stress_proj"], 4),
        "metrics_s": round(first_stage - problem_at, 4),
        "stages_s": round(last_end - first_stage, 4),
        "post_s": round(t1 - last_end, 4),
        "stages": stages_out,
        "candidates": sm.get("candidate_count"),
        "integer_variables": sm.get("integer_variables"),
        "scalar_variables": sm.get("scalar_variables"),
        "stress_rows_input": sm.get("stress_rows_input"),
        "stress_rows_model": sm.get("stress_rows_model"),
        "accepted_stage": sm.get("accepted_stage"),
        "ranking_complete": sm.get("ranking_complete"),
        "expected_score": round(target.expected_score, 4),
        "current_score": round(target.current_score, 4),
        "holdings": {str(k): v for k, v in sorted(target.quantities.items())},
    }


def summarize_build(rec, scen):
    phases = {k: round(v, 4) for k, v in rec["phases"].items()}
    total = rec["t1"] - rec["t0"]
    accounted = sum(rec["phases"].values())
    price = {k: {"calls": v[0], "s": round(v[1], 4)} for k, v in rec["price"].items()}
    return {
        "build_s": round(total, 4),
        "phases": phases,
        "inline_s": round(total - accounted, 4),  # market-stress loop, normals, ScenarioSet args, ...
        "price_calls": sum(v[0] for v in rec["price"].values()),
        "price_s": round(sum(v[1] for v in rec["price"].values()), 4),
        "price_by_phase": price,
        "ids": len(scen.ids),
        "options": None,
        "scenario_rows": int(len(scen.weights)),
        "stress_rows": int(len(scen.stress)),
    }


# --------------------------------------------------------------------------------------------
# Model size attribution (--sizes): variables and constraints by creating line in optimizer.py
# --------------------------------------------------------------------------------------------
def install_size_tracking():
    from cvxpy.constraints.constraint import Constraint
    from cvxpy.expressions.variable import Variable

    target_file = str(Path(optmod.__file__).resolve())
    created = {}

    def where():
        f = sys._getframe(2)
        while f is not None:
            if str(Path(f.f_code.co_filename).resolve()) == target_file:
                return f"{f.f_code.co_name}:{f.f_lineno}"
            f = f.f_back
        return "?"

    for cls in (Constraint, Variable):
        original = cls.__init__

        def init(self, *a, __original=original, **k):
            __original(self, *a, **k)
            created[id(self)] = where()

        cls.__init__ = init
    return created


def size_report(problem, created):
    from cvxpy import settings as s

    var_groups = defaultdict(lambda: {"vars": 0, "scalars": 0, "integer": 0})
    for v in problem.variables():
        g = var_groups[created.get(id(v), "?")]
        g["vars"] += 1
        g["scalars"] += v.size
        if v.attributes["integer"] or v.attributes["boolean"]:
            g["integer"] += v.size
    con_groups = defaultdict(list)
    for c in problem.constraints:
        con_groups[created.get(id(c), "?")].append(c)
    rows = {}
    for where, group in con_groups.items():
        data, _, _ = cp.Problem(cp.Minimize(0), group).get_problem_data(cp.HIGHS)
        a = data[s.A]
        rows[where] = {"constraints": len(group), "scalar_rows_pre": int(sum(c.size for c in group)),
                       "rows": int(a.shape[0]), "nnz": int(a.nnz), "cols_touched": int(a.shape[1])}
    return {"variables": dict(var_groups), "constraints": rows}


# --------------------------------------------------------------------------------------------
def fmt(values):
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    med = statistics.median(values)
    if len(values) == 1:
        return f"{med:.3f}"
    return f"{med:.3f} [{min(values):.3f}-{max(values):.3f}]"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cands", default="12,14,17,20,26,38")
    p.add_argument("--clocks", default="10:00,14:00,15:40")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--stock-only", default="12,38", help="candidate counts that also get a stock_only solve")
    p.add_argument("--stock-only-clocks", default="10:00")
    p.add_argument("--no-highs-log", action="store_true")
    p.add_argument("--sizes", action="store_true", help="per-line model size attribution (adds overhead)")
    p.add_argument("--dump-mps", default=None)
    p.add_argument("--burn-surplus", action="store_true")
    p.add_argument("--policy", default="", help="k=v,... policy overrides (e.g. solver_seconds=10)")
    p.add_argument("--out", default=str(LANE / "results.jsonl"))
    p.add_argument("--tag", default="")
    p.add_argument("--no-solve", action="store_true")
    p.add_argument("--no-build-timing", action="store_true", help="build once per case only")
    args = p.parse_args()
    STATE["highs_log"] = not args.no_highs_log
    STATE["dump_mps"] = args.dump_mps
    install()
    created = install_size_tracking() if args.sizes else None
    policy = {}
    for item in filter(None, args.policy.split(",")):
        k, v = item.split("=")
        policy[k] = float(v)
    load = os.getloadavg()
    env = {"src": SRC, "tag": args.tag, "load_at_start": [round(x, 2) for x in load], "pid": os.getpid(),
           "cvxpy": cp.__version__, "highspy": __import__("importlib.metadata").metadata.version("highspy")}
    print("ENV", json.dumps(env), flush=True)
    from hedge.optimizer import HedgeOptimizer
    from hedge.scenarios import ScenarioEngine
    from hedge.universe import screen_options

    counts = [int(x) for x in args.cands.split(",")]
    clocks = args.clocks.split(",")
    stock_counts = {int(x) for x in args.stock_only.split(",") if x}
    stock_clocks = set(args.stock_only_clocks.split(","))
    setups = {}
    for n in counts:
        for clock in clocks:
            m, quotes, snap, prev, nav, now = cases.build_case(n, clock, args.burn_surplus, policy=policy)
            ids = screen_options(m, quotes, snap.quantities())
            setups[(n, clock)] = {"m": m, "quotes": quotes, "snap": snap, "prev": prev, "nav": nav, "now": now,
                                  "ids": ids, "engine": ScenarioEngine(m), "opt": HedgeOptimizer(m), "builds": 0}
    rows = []
    out = open(args.out, "a")

    def emit(rec):
        rec = {**rec, "tag": args.tag, "src": SRC, "load1": round(os.getloadavg()[0], 2)}
        rows.append(rec)
        line = json.dumps(rec)
        out.write(line + "\n")
        out.flush()
        brief = {k: rec[k] for k in rec if k not in ("stages", "holdings", "price_by_phase", "phases", "sizes")}
        print(json.dumps(brief), flush=True)

    for rep in range(args.reps):
        for (n, clock), su in setups.items():
            m = su["m"]
            STATE["case"] = f"n{n}_{clock.replace(':', '')}_r{rep}"
            STATE["stage_no"] = 0
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if rep == 0 or not args.no_build_timing:
                    scen = su["engine"].build(su["snap"], su["quotes"], su["now"], su["ids"])
                    su["scen"] = scen
                    b = summarize_build(LAST["build"], scen)
                    b["options"] = sum(m.contract_map[i].kind == "OPT" for i in scen.ids)
                    emit({"kind": "build", "n": n, "clock": clock, "rep": rep, "cold": su["builds"] == 0, **b})
                    su["builds"] += 1
                if args.no_solve:
                    continue
                modes = [False] + ([True] if (n in stock_counts and clock in stock_clocks) else [])
                for stock_only in modes:
                    STATE["case"] = f"n{n}_{clock.replace(':', '')}_r{rep}_{'stk' if stock_only else 'full'}"
                    STATE["stage_no"] = 0
                    target = su["opt"].solve(su["snap"], su["quotes"], su["scen"], su["prev"], su["nav"], stock_only)
                    rec = LAST["solve"]
                    srec = summarize_solve(rec, target)
                    srec["solver_total_ms"] = target.solver_metrics.get("total_ms")
                    if created is not None and rec.get("problem") is not None and not stock_only:
                        srec["sizes"] = size_report(rec["problem"], created)
                    emit({"kind": "solve", "n": n, "clock": clock, "rep": rep,
                          "mode": "stock_only" if stock_only else "full", **srec})
    out.close()
    print_summary(rows)


def print_summary(rows):
    print("\n=== SCENARIO BUILD (s): median [min-max]; cold = first build of the engine ===")
    keys = ["refresh_daily", "trading_variance", "covariance", "market_betas", "reprice_changes", "reprice_next",
            "reprice_overnight", "stress_matrix", "rhos", "digest", "scenarioset_validate"]
    groups = defaultdict(list)
    for r in rows:
        if r["kind"] == "build":
            groups[(r["n"], r["clock"], "cold" if r["cold"] else "warm")].append(r)
    for key, rs in sorted(groups.items()):
        parts = " ".join(f"{k}={fmt([x['phases'].get(k, 0.0) for x in rs])}" for k in keys if any(k in x["phases"] for x in rs))
        print(f"n={key[0]:>2} {key[1]} {key[2]}: build={fmt([x['build_s'] for x in rs])} price_calls={rs[0]['price_calls']}"
              f" price_s={fmt([x['price_s'] for x in rs])} inline={fmt([x['inline_s'] for x in rs])} | {parts}")
    print("\n=== SOLVE (s): median [min-max] ===")
    sgroups = defaultdict(list)
    for r in rows:
        if r["kind"] == "solve":
            sgroups[(r["n"], r["clock"], r["mode"])].append(r)
    for key, rs in sorted(sgroups.items()):
        statuses = sorted({x["status"] for x in rs})
        print(f"n={key[0]:>2} {key[1]} {key[2]:<10} status={statuses} total={fmt([x['total_s'] for x in rs])} "
              f"construct={fmt([x['construct_s'] for x in rs])} (risk={fmt([x['risk_eval_s'] for x in rs])}) "
              f"metrics={fmt([x['metrics_s'] for x in rs])} stages={fmt([x['stages_s'] for x in rs])} post={fmt([x['post_s'] for x in rs])}"
              f" ints={rs[0]['integer_variables']} scalars={rs[0]['scalar_variables']} stress_rows={rs[0]['stress_rows_model']}")
        nst = max(len(x["stages"]) for x in rs)
        for k in range(nst):
            sts = [x["stages"][k] for x in rs if len(x["stages"]) > k]
            h = [s["highs"] or {} for s in sts]
            mdl = sts[0]["model"] or {}
            print(f"      {sts[0]['stage']:<10} canon={fmt([s['canon_s'] for s in sts])} solve_gpd={fmt([s['solve_gpd_s'] for s in sts])}"
                  f" passModel={fmt([s['passModel_s'] for s in sts])} run={fmt([s['run_s'] for s in sts])}"
                  f" highs_ovh={fmt([s.get('highs_overhead_s') for s in sts])} unpack={fmt([s['unpack_s'] for s in sts])}"
                  f" | presolve={fmt([x.get('t_presolve') for x in h])} nodes={[x.get('nodes') for x in h]}"
                  f" gap={[x.get('gap_text') for x in h]} lp_it={[x.get('lp_iters') for x in h]} "
                  f"model={mdl.get('rows')}x{mdl.get('cols')} nnz={mdl.get('nnz')} int={mdl.get('int_cols')}"
                  f" presolved={h[0].get('presolved')}")


if __name__ == "__main__":
    main()

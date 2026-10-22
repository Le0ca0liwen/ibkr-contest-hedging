"""Real-path replay of one session through the REAL TradingService loops (spread_model lane).

Adapted from the contest-day lane simulator (research/contest_day/sim.py lineage) with these changes:
  * market: real 1-minute paths (common.ReplayMarket), relative to the previous official close and
    anchored at today's spots; option prices from the app's ValuationEngine at per-contract chain/today
    IVs; real relative option spreads; prices move on a 5-second grid (observed_at refreshes every 1 s);
  * clock: discrete-event and FROZEN while anything computes (solves/scenario builds take zero sim time:
    "computing load is not a problem"), so results do not depend on this machine's load;
  * fills: the demo broker's instant mid fills are replaced by a fill model run every simulated second:
      marketable limit/market -> fill at the touch; passive limit inside the spread -> 'touch' never fills
      (IBKR-paper-like), 'poisson' fills at the limit with a hazard that rises toward the touch
      (common random numbers per second and contract, identical across variants);
  * monitor-loop scenario builds are lazy: rebuilt when a solve is due, a plan is pending, an order is
    working or the last build is older than 60 s; otherwise the previous set is reused (execution paths
    still rebuild through ScenarioSet.is_current exactly as in the app);
  * starts from the mandated book + H0 (one shared Day-0 15:44 app solve) with the previous close NAV
    = $1,000,000 at today's spots; the session is contest day 2026-10-22 (target = 1e6 x (1+r/360)).
Nothing touches IBKR/Moomoo; nothing is written to the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time as _time
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--name", required=True)
p.add_argument("--day", required=True)
p.add_argument("--repo", default="base",
               help="base = $REPO (default: this repository); proto = $PROTO_REPO (default: spread_model/proto, built by make_proto.sh); anything else = a path to an app source tree")
p.add_argument("--policy", default="{}")
p.add_argument("--execution", default="{}")
p.add_argument("--fill", default="poisson", choices=["poisson", "touch", "instant"])
p.add_argument("--start", default="09:30:00")
p.add_argument("--end", default="16:00:00")
_RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
p.add_argument("--out", default=str(_RESEARCH_ROOT / "spread_model" / "runs"))
p.add_argument("--grid", type=int, default=5)
p.add_argument("--lam-mid-opt-spy", type=float, default=0.40, help="P(fill within 60 s) at mid, SPY options")
p.add_argument("--lam-mid-opt", type=float, default=0.20, help="P(fill within 60 s) at mid, SPCX/RKLB options")
p.add_argument("--lam-mid-stk", type=float, default=0.50, help="P(fill within 15 s) at mid, stocks")
p.add_argument("--kappa", type=float, default=5.5, help="hazard x exp(kappa*(x-0.5)); x=0 bid side,1 touch")
p.add_argument("--time-scale", type=float, default=4.0,
               help="solver real-time budgets x this factor (compensates this shared machine's CPU load)")
ARGS = p.parse_args()


def _repo_dir(name):
    if name == "base":  # the baseline app source (the study used commit 92b4121)
        return os.environ.get("REPO") or str(_RESEARCH_ROOT.parent)
    if name == "proto":  # baseline + spread_aware_proto.patch
        return os.environ.get("PROTO_REPO") or str(_RESEARCH_ROOT / "spread_model" / "proto")
    return str(Path(name).resolve())


os.environ["REPO"] = _repo_dir(ARGS.repo)

import numpy as np  # noqa: E402

from common import (  # noqa: E402
    CLOSE, DAY_ZERO, LANE, NY, OPEN, ReplayMarket, build_manifest,
)

import hedge.burner_service as burn_mod  # noqa: E402
import hedge.closing as closing_mod  # noqa: E402
import hedge.execution as exec_mod  # noqa: E402
import hedge.service as svc_mod  # noqa: E402
from hedge.data import QuoteRecorder  # noqa: E402
from hedge.service import TradingService  # noqa: E402
from hedge.storage import Store  # noqa: E402
import fastval  # noqa: E402
import hedge.optimizer as opt_mod  # noqa: E402
import cvxpy as cp  # noqa: E402

fastval.install()
# Load compensation: the optimizer's wall-clock budgets (solver_seconds, ranking_seconds, HiGHS
# time_limit) run on a clock slowed by --time-scale, so a busy shared CPU does not turn into solver
# timeouts or skipped ranking stages that an unloaded operator machine would not see.
_K = max(1.0, ARGS.time_scale)
_real_monotonic = _time.monotonic
opt_mod.monotonic = lambda: _real_monotonic() / _K
_orig_problem_solve = cp.Problem.solve


def _scaled_problem_solve(self, *a, **k):
    options = k.get("highs_options")
    if options and options.get("time_limit") is not None:
        k = {**k, "highs_options": {**options, "time_limit": options["time_limit"] * _K}}
    return _orig_problem_solve(self, *a, **k)


cp.Problem.solve = _scaled_problem_solve

EPOCH = datetime(2026, 10, 1, tzinfo=NY)


class SimTime:
    """Discrete-event clock; frozen while any managed task computes."""

    def __init__(self, start):
        self.t = start
        self.sleepers = {}
        self.managed = set()
        self.jumped = 0.0
        self.queue = None

    def now(self):
        return self.t

    def mono(self):
        return 1e6 + (self.t - EPOCH).total_seconds()

    async def sleep(self, seconds, event=None):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        key = object()
        self.sleepers[key] = (self.t + timedelta(seconds=max(0.0, float(seconds))), fut, event, asyncio.current_task())
        try:
            await fut
        finally:
            self.sleepers.pop(key, None)

    async def run(self):
        real_sleep = asyncio.sleep
        while True:
            await real_sleep(0.0003)
            for wake, fut, event, _ in list(self.sleepers.values()):
                if not fut.done() and (wake <= self.t or (event is not None and event.is_set())):
                    fut.set_result(None)
            pending = [(w, t) for (w, f, e, t) in self.sleepers.values() if not f.done()]
            parked = {t for _, t in pending}
            live = {t for t in self.managed if not t.done()}
            if not pending or not live.issubset(parked):
                continue
            if self.queue is not None and self.queue._unfinished_tasks:
                continue
            wake = min(w for w, _ in pending)
            delta = (wake - self.t).total_seconds()
            if delta > 0:
                self.t = wake
                self.jumped += delta


class TimeShim:
    def __init__(self, sim):
        self._sim = sim

    def monotonic(self):
        return self._sim.mono()

    def __getattr__(self, name):
        return getattr(_time, name)


class AsyncioShim:
    """asyncio for the app modules: sim sleeps, and to_thread runs inline (zero sim time, no threads)."""

    def __init__(self, sim):
        self._sim = sim

    async def sleep(self, seconds, result=None):
        await self._sim.sleep(seconds)
        return result

    async def to_thread(self, func, *a, **k):
        return func(*a, **k)

    def __getattr__(self, name):
        return getattr(asyncio, name)


class Log:
    def __init__(self, path):
        self.file = open(path, "w")

    def __call__(self, kind_, **values):
        self.file.write(json.dumps({"ev": kind_, **values}, default=str) + "\n")


def fill_hazards(contract):
    if contract.kind == "STK":
        return -math.log(1 - ARGS.lam_mid_stk) / 15.0
    prob = ARGS.lam_mid_opt_spy if contract.symbol == "SPY" else ARGS.lam_mid_opt
    return -math.log(1 - prob) / 60.0


async def run():
    day = ARGS.day
    start = datetime.combine(OPEN.date(), datetime.strptime(ARGS.start, "%H:%M:%S").time(), NY)
    end = datetime.combine(OPEN.date(), datetime.strptime(ARGS.end, "%H:%M:%S").time(), NY)
    out = Path(ARGS.out) / ARGS.name
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        if f.is_file():
            f.unlink()
    manifest = build_manifest(json.loads(ARGS.policy), json.loads(ARGS.execution))
    log = Log(out / "log.jsonl")
    log("manifest", repo=ARGS.repo, day=day, policy=manifest.policy.model_dump(mode="json"),
        execution=manifest.execution.model_dump(mode="json"), fill=ARGS.fill, argv=sys.argv)

    sim = SimTime(start)
    shim_t, shim_a = TimeShim(sim), AsyncioShim(sim)
    for mod in (svc_mod, burn_mod):
        mod.time = shim_t
    for mod in (svc_mod, burn_mod, closing_mod, exec_mod):
        mod.asyncio = shim_a

    market = ReplayMarket(manifest, day)
    grid = max(1, ARGS.grid)

    block_cache = {}

    def quotes_at(now):
        k = int((now - OPEN).total_seconds())
        base = OPEN + timedelta(seconds=(k // grid) * grid)
        if block_cache.get("base") != base:
            block_cache["base"], block_cache["quotes"] = base, market.quotes(base)
        if block_cache.get("now") != now:
            block_cache["now"] = now
            block_cache["stamped"] = {
                i: q.model_copy(update={"observed_at": now, "greek_at": now if q.greek_at else None})
                for i, q in block_cache["quotes"].items()
            }
        return block_cache["stamped"]

    svc_mod.demo_quotes = lambda m, now, elapsed=0: quotes_at(now)

    shm = Path("/dev/shm/spread_model") / ARGS.name
    shm.mkdir(parents=True, exist_ok=True)
    for f in shm.glob("*"):
        f.unlink()
    store = Store(f"sqlite:///{shm}/hedge.db")
    from sqlalchemy import event as sa_event

    @sa_event.listens_for(store.engine, "connect")
    def _fast_sqlite(dbapi_connection, record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA synchronous=OFF")
        cur.execute("PRAGMA journal_mode=MEMORY")
        cur.close()

    store.initialize()
    service = TradingService(manifest, store, demo=True)
    service.now = sim.now
    store.clock = sim.now
    service.execution.clock = sim.now
    service.closing_recorder.clock = sim.now
    service.close_marks.clock = sim.now
    service.recorder = QuoteRecorder(str(out / "quotes"))
    service.recorder.append = lambda *a, **k: None  # no per-iteration quote archive (speed)
    sim.queue = service.events
    broker = service.broker

    # Starting book: mandated + H0; previous close NAV = $1,000,000 at today's spots (Day-0 16:00 marks).
    h0 = {int(k): v for k, v in json.loads((LANE / "h0.json").read_text()).items()}
    broker.positions = dict(h0)
    prev = market.prev_marks()
    cash = 1_000_000.0 - sum(prev[i] * q * manifest.contract_map[i].multiplier for i, q in h0.items())
    quotes = quotes_at(sim.now())
    broker.cash = Decimal(str(cash))
    broker.nav = Decimal(str(cash + sum(quotes[i].mid * q * manifest.contract_map[i].multiplier for i, q in h0.items())))
    broker._quotes, broker.now = quotes, sim.now()
    d0 = Decimal("1000000.00")
    service.ledger.import_close(DAY_ZERO, d0, "replay previous close (book at Day-0 marks)", manifest.account)
    target = float(service.ledger.target_for(OPEN.date()))
    log("baseline", d0=d0, target=target, open_nav=float(broker.nav), h0=h0)

    service.store.record("configuration", manifest.account, manifest, manifest.config_hash)
    service.confirm_configuration(manifest.config_hash)
    service.burner.restore_setting()
    service.restore_autostart()

    # ---------------------------------------------------------------- instrumentation
    fills = []
    original_execute = broker._execute

    def fee_of(c, signed, price):
        rate = manifest.policy.commission_option if c.kind == "OPT" else manifest.policy.commission_share
        fee = max(manifest.policy.minimum_commission, abs(signed) * rate)
        return min(fee, 0.01 * abs(signed) * price) if c.kind == "STK" else fee

    def logged_execute(order_id, con_id, signed, price, quote, order_ref):
        c = manifest.contract_map[con_id]
        row = {
            "at": sim.now().isoformat(), "sec": (sim.now() - OPEN).total_seconds(), "order_id": order_id,
            "con_id": con_id, "label": c.label, "kind": c.kind, "qty": signed, "price": float(price),
            "mid": quote.mid, "bid": quote.bid, "ask": quote.ask,
            "spread_paid": (float(price) - quote.mid) * signed * c.multiplier,
            "fee": fee_of(c, signed, float(price)),
            "order_ref": order_ref,
            "decision_id": service.last_decision_id,
        }
        fills.append(row)
        log("fill", **row)
        return original_execute(order_id, con_id, signed, price, quote, order_ref)

    broker._execute = logged_execute
    original_submit = broker.submit

    async def logged_submit(request, order_id):
        c = manifest.contract_map[request.con_id]
        q = broker.quotes().get(request.con_id)
        log("submit", at=sim.now().isoformat(), order_id=order_id, con_id=request.con_id, label=c.label, kind=c.kind,
            qty=request.signed_quantity, limit=float(request.limit_price) if request.limit_price is not None else None,
            type=request.order_type, urgency=request.urgency, bid=q.bid if q else None, ask=q.ask if q else None,
            decision_id=request.decision_id)
        return await original_submit(request, order_id)

    broker.submit = logged_submit
    original_replace = broker.replace

    async def logged_replace(order_id, limit_price):
        log("replace", at=sim.now().isoformat(), order_id=order_id, limit=float(limit_price))
        return await original_replace(order_id, limit_price)

    broker.replace = logged_replace
    original_cancel = broker.cancel

    async def logged_cancel(order_id):
        log("cancel", at=sim.now().isoformat(), order_id=order_id)
        return await original_cancel(order_id)

    broker.cancel = logged_cancel

    # Fill model: the monitor's demo "fill everything at the limit" is disabled; the streamer fills.
    original_fill = broker.fill

    async def no_fill(order_id, quantity=None):
        return None

    broker.fill = no_fill

    # Lazy monitor builds (see module docstring).
    original_build = service.scenarios.build
    cache = {"set": None}
    builds = {"real": 0, "reused": 0}

    def lazy_build(snapshot, quotes, now, ids=None):
        cur = cache["set"]
        need = (
            cur is None
            or tuple(sorted(ids or quotes.keys())) != cur.ids
            or (now - cur.built_at).total_seconds() > 60
            or service.pending_target is not None
            or broker.orders
            or store.intents(manifest.account, True)
            or service.force_optimize
            or (service.optimization_due() and not service.execution.burn_open())
        )
        if need:
            cache["set"] = original_build(snapshot, quotes, now, ids)
            builds["real"] += 1
        else:
            builds["reused"] += 1
        return cache["set"]

    service.scenarios.build = lazy_build

    solves = []
    original_solve = service.optimizer.solve

    def timed_solve(*a, **k):
        t0 = _time.perf_counter()
        target = original_solve(*a, **k)
        stock_only = a[5] if len(a) > 5 else k.get("stock_only", False)
        solves.append({"at": sim.now().isoformat(), "wall_s": round(_time.perf_counter() - t0, 3),
                       "status": target.status, "stock_only": bool(stock_only)})
        return target

    service.optimizer.solve = timed_solve

    # Diagnostics: every slice attempt's outcome, and why pending plans end.
    original_submit_slice = service.execution.submit_slice

    async def logged_submit_slice(target, decision_id, *a, **k):
        result = await original_submit_slice(target, decision_id, *a, **k)
        log("slice", at=sim.now().isoformat(), decision_id=decision_id, submitted=result.get("submitted"),
            reason=(result.get("reason") or "")[:160])
        return result

    service.execution.submit_slice = logged_submit_slice
    original_econ = service.execution.economic_check
    from hedge.models import Target as _Target
    from hedge.optimizer import HedgeOptimizer as _Opt

    def logged_econ(snapshot, quotes, scenarios, quantities, now):
        ok = original_econ(snapshot, quotes, scenarios, quantities, now)
        try:
            held = snapshot.quantities()
            change = {str(i): quantities.get(i, 0) - held.get(i, 0) for i in set(quantities) | set(held)
                      if quantities.get(i, 0) != held.get(i, 0)}
            previous, estimate = service.execution.score_context(snapshot, quotes, now)
            t = _Target(quantities=quantities, expected_score=0, current_score=0, uncertainty=0, status="SLICE",
                        snapshot_id=snapshot.version, scenario_id=scenarios.identifier, config_hash=manifest.config_hash)
            ev = _Opt(manifest).evaluate_target(snapshot, quotes, scenarios, t, previous, estimate)
            ex = _Opt(manifest).evaluate_target(snapshot, quotes, scenarios, t, previous, estimate, include_costs=False)
            log("econ", at=sim.now().isoformat(), change=change, exposure=round(ex.current_score - ex.expected_score, 3),
                net=round(ev.current_score - ev.expected_score, 3), se=round(ev.uncertainty, 3),
                cost=round(ev.estimated_cost, 3), ok=ok)
        except Exception as exc:  # diagnostics must never change the run
            log("econ_error", error=repr(exc))
        return ok

    service.execution.economic_check = logged_econ
    original_execute_plan = service._execute_pending_plan

    async def logged_execute_plan():
        before = service.pending_target
        decision = service.last_decision_id
        await original_execute_plan()
        if before is not None and service.pending_target is None:
            log("plan_end", at=sim.now().isoformat(), decision_id=decision, health=service.health[:200],
                started=service.active_plan_started)

    service._execute_pending_plan = logged_execute_plan

    async def monitor_pause():
        await sim.sleep(manifest.policy.risk_seconds, service.monitor_wakeup)
        service.monitor_wakeup.clear()

    service._monitor_pause = monitor_pause

    async def burner_wait(timeout=1.0):
        await sim.sleep(timeout, service.burner.wakeup)
        service.burner.wakeup.clear()

    service.burner.wait = burner_wait

    # Common random numbers: one uniform per (second, contract), identical for every variant.
    rng = np.random.default_rng(int(day.replace("-", "")))
    ids_sorted = sorted(manifest.contract_map)
    uniforms = rng.random((23401 + 120, len(ids_sorted)))
    col = {i: j for j, i in enumerate(ids_sorted)}

    async def fill_step(now):
        sec = int((now - OPEN).total_seconds())
        for oid, order in list(broker.orders.items()):
            if isinstance(order, tuple):
                continue
            request = broker.requests.get(oid)
            q = broker.quotes().get(order.con_id)
            if request is None or q is None:
                continue
            signed = order.signed_remaining
            c = manifest.contract_map[order.con_id]
            if request.order_type == "MKT":
                await original_fill(oid)
                continue
            limit = float(request.limit_price)
            marketable = limit >= q.ask - 1e-9 if signed > 0 else limit <= q.bid + 1e-9
            if marketable:
                await original_fill(oid)
                continue
            inside = q.bid + 1e-9 < limit < q.ask - 1e-9
            if not inside or ARGS.fill == "touch":
                continue
            if ARGS.fill == "instant":
                await original_fill(oid)
                continue
            spread = q.ask - q.bid
            x = (limit - q.bid) / spread if signed > 0 else (q.ask - limit) / spread
            hazard = fill_hazards(c) * math.exp(ARGS.kappa * (x - 0.5))
            if uniforms[min(sec, len(uniforms) - 1), col[order.con_id]] < 1 - math.exp(-hazard):
                await original_fill(oid)

    async def streamer():
        last_min = -1
        while True:
            now = sim.now()
            qs = quotes_at(now)
            broker.update_quotes(qs, now)
            if service.mode == "PAPER":
                await fill_step(now)
            minute = int((now - OPEN).total_seconds() // 60)
            if minute != last_min:
                last_min = minute
                snap = broker.snapshot()
                log("tick", at=now.isoformat(), mode=service.mode, health=service.health[:160], nav=float(snap.nav),
                    pos={str(i): q for i, q in snap.quantities().items()}, pending=service.pending_target is not None,
                    orders=len(snap.orders))
            if now >= end:
                return
            await sim.sleep(1.0)

    tasks = {
        "events": asyncio.create_task(service._event_loop()),
        "monitor": asyncio.create_task(service._monitor_loop()),
        "burner": asyncio.create_task(service._burner_loop()),
    }
    stream = asyncio.create_task(streamer())
    sim.managed = {tasks["monitor"], tasks["burner"], stream}
    clock_task = asyncio.create_task(sim.run())
    wall0 = _time.perf_counter()
    try:
        await stream
    finally:
        for t in [*tasks.values(), clock_task]:
            t.cancel()
        await asyncio.gather(*tasks.values(), clock_task, return_exceptions=True)
        for name, t in tasks.items():
            if t.done() and not t.cancelled() and t.exception():
                log("task_exception", task=name, error=repr(t.exception()))

    # ---------------------------------------------------------------- close accounting
    marks = market.close_marks()
    snap = broker.snapshot()
    positions = snap.quantities()
    nav_close = float(broker.cash + broker.accrued_cash) + sum(
        marks[i] * q * manifest.contract_map[i].multiplier for i, q in positions.items()
    )
    # No-trade reference: H0 held all day (same cash, same carry approximation).
    nav_hold = cash + float(broker.accrued_cash) + sum(marks[i] * q * manifest.contract_map[i].multiplier for i, q in h0.items())
    decisions = store.records("decision", manifest.account, 100000)
    waits = store.records("execution_wait", manifest.account, 100000)
    report = {
        "name": ARGS.name, "day": day, "repo": ARGS.repo, "fill": ARGS.fill,
        "policy": json.loads(ARGS.policy), "execution": json.loads(ARGS.execution),
        "target": target, "nav_close": nav_close, "miss": abs(nav_close - target), "gap": nav_close - target,
        "nav_hold": nav_hold, "miss_hold": abs(nav_hold - target),
        "final_positions": {manifest.contract_map[i].label: q for i, q in positions.items()},
        "fills": fills,
        "spread_paid": sum(f["spread_paid"] for f in fills),
        "fees": sum(f["fee"] for f in fills),
        "mode": service.mode, "health": service.health,
        "solves": len(solves), "solve_wall": sum(s["wall_s"] for s in solves),
        "builds": builds, "wall_s": round(_time.perf_counter() - wall0, 1),
        "decisions": [
            {k: d.get(k) for k in ("at", "status", "trade_eligible", "improvement", "threshold", "optimization_scope",
                                   "nav_gap", "quantities", "estimated_cost", "uncertainty", "corrective",
                                   "current_score", "expected_score", "blocking_reasons", "components", "gross_improvement",
                                   "reversal_reserve", "id")}
            for d in decisions
        ],
        "decision_ids": [d.get("id") for d in decisions],
        "execution_waits": [{"at": w.get("at"), "reason": w.get("reason")} for w in waits],
        "close_marks": {str(k): v for k, v in marks.items()},
    }
    (out / "report.json").write_text(json.dumps(report, default=str))
    store.close()
    for f in shm.glob("*"):
        f.unlink()
    print(json.dumps({"name": ARGS.name, "miss": round(report["miss"], 1), "gap": round(report["gap"], 1),
                      "miss_hold": round(report["miss_hold"], 1), "fills": len(fills),
                      "spread_paid": round(report["spread_paid"], 1), "fees": round(report["fees"], 1),
                      "solves": len(solves), "builds": builds, "wall_s": report["wall_s"], "mode": service.mode}))


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception:
        traceback.print_exc()
        raise

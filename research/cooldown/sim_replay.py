"""Cooldown lane: replay a REAL session through the REAL TradingService loops.

Adapted from research/contest_day/sim.py:
  * discrete-event clock, FROZEN while code computes (sim time advances only when every loop is
    parked in its sleep), so results do not depend on this shared machine's load;
  * module-level time.monotonic / asyncio.sleep in hedge.service / burner_service / closing shimmed;
  * quotes replay the real 1-minute stock paths (relative moves from today's spots, linear within the
    minute), options are priced with the app's ValuationEngine at each contract's chain/today IV
    (precomputed per second by precompute.py) with the contract's real relative spread;
  * fills: --fill mid   = the SimBroker as shipped (a limit at or inside the touch fills at its limit);
           --fill touch = IBKR-paper-like: a limit fills only when marketable, at the bid/ask.
Nothing touches IBKR/Moomoo; nothing is written to the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time as _time
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LANE, NY, OPTIONS, SIM_DAY, STOCK_SPREAD, build_manifest, history_file, np  # noqa: E402

import hedge.burner_service as burn_mod  # noqa: E402
import hedge.closing as closing_mod  # noqa: E402
import hedge.service as svc_mod  # noqa: E402
from hedge.data import QuoteRecorder  # noqa: E402
from hedge.models import Quote  # noqa: E402
from hedge.service import TradingService  # noqa: E402
from hedge.storage import Store  # noqa: E402

import fastval  # noqa: E402

fastval.install()  # bit-identical cached-QuantLib ValuationEngine.price (see fastval.py)
import vecscen  # noqa: E402

vecscen.install()  # numpy twin of QL 1.43 BjS for scenario/stress repricing (<= $1e-10, probe/vecscen_check.py)

EPOCH = datetime(2026, 10, 1, tzinfo=NY)
STOCK_IDS = {"SPCX": 10, "RKLB": 20, "SPY": 30}


class SimTime:
    def __init__(self, start, frozen=True):
        self.sim_base = start
        self.frozen = frozen
        self.real_base = _time.monotonic()
        self.sleepers = {}
        self.managed = set()
        self.jumped = 0.0
        self.queue = None

    def now(self):
        if self.frozen:
            return self.sim_base
        return self.sim_base + timedelta(seconds=_time.monotonic() - self.real_base)

    def mono(self):
        return 1e6 + (self.now() - EPOCH).total_seconds()

    async def sleep(self, seconds, event=None):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        key = object()
        self.sleepers[key] = (self.now() + timedelta(seconds=max(0.0, float(seconds))), fut, event,
                              asyncio.current_task())
        try:
            await fut
        finally:
            self.sleepers.pop(key, None)

    async def run(self):
        real_sleep = asyncio.sleep
        while True:
            await real_sleep(0.0002)
            now = self.now()
            for wake, fut, event, _ in list(self.sleepers.values()):
                if not fut.done() and (wake <= now or (event is not None and event.is_set())):
                    fut.set_result(None)
            pending = [(w, t) for (w, f, e, t) in self.sleepers.values() if not f.done()]
            parked = {t for _, t in pending}
            live = {t for t in self.managed if not t.done()}
            if not pending or not live.issubset(parked):
                continue
            if self.queue is not None and self.queue._unfinished_tasks:
                continue
            wake = min(w for w, _ in pending)
            delta = (wake - self.now()).total_seconds()
            if delta > 0:
                self.sim_base += timedelta(seconds=delta)
                self.jumped += delta


class TimeShim:
    def __init__(self, sim):
        self._sim = sim

    def monotonic(self):
        return self._sim.mono()

    def __getattr__(self, name):
        return getattr(_time, name)


class AsyncioShim:
    def __init__(self, sim):
        self._sim = sim

    async def sleep(self, seconds, result=None):
        await self._sim.sleep(seconds)
        return result

    def __getattr__(self, name):
        return getattr(asyncio, name)


def quote_from_price(price, rel_spread, tick=0.01, min_spread=0.01):
    half = max(min_spread / 2, 0.5 * rel_spread * price)
    bid = max(tick, math.floor((price - half) / tick + 1e-9) * tick)
    ask = math.ceil((price + half) / tick - 1e-9) * tick
    return round(bid, 4), round(ask, 4)


class ReplayMarket:
    def __init__(self, manifest, npz_path, spread_scale=1.0):
        z = np.load(npz_path)
        self.m = manifest
        self.base = datetime.combine(SIM_DAY, datetime.min.time(), NY).replace(hour=9, minute=30)
        self.ids = [int(i) for i in z["ids"]]
        self.price = z["price"]
        self.greeks = z["greeks"]
        self.mid = {s: z[f"mid_{s}"] for s in STOCK_IDS}
        self.close_spot = dict(zip(("SPCX", "RKLB", "SPY"), z["close_spot"], strict=True))
        self.close_price = dict(zip(self.ids, z["close_price"], strict=True))
        self.iv = dict(zip(self.ids, z["iv"], strict=True))
        self.rel_spread = {i: float(s) * spread_scale for i, s in zip(self.ids, z["rel_spread"], strict=True)}
        self.n = len(self.price)
        self.cache = {}

    def k(self, now):
        return int(min(max((now - self.base).total_seconds(), 0), self.n - 1))

    def quotes(self, now):
        k = self.k(now)
        hit = self.cache.get(k)
        if hit is not None and hit[0] == now:
            return hit[1]
        out = {}
        for s, cid in STOCK_IDS.items():
            mid = float(self.mid[s][k])
            spread = STOCK_SPREAD[s]
            bid = round(mid - spread / 2, 2)
            ask = round(bid + spread, 2)
            out[cid] = Quote(provider="synthetic", con_id=cid, bid=bid, ask=ask, bid_size=500, ask_size=500,
                             observed_at=now, data_type="synthetic", spot=mid, model_price=mid,
                             shortable_shares=None)
        g = k // 5
        for j, cid in enumerate(self.ids):
            c = self.m.contract_map[cid]
            p = float(self.price[k, j])
            bid, ask = quote_from_price(p, self.rel_spread[cid])
            d, gm, v, th, rh = (float(x) for x in self.greeks[g, j])
            out[cid] = Quote(provider="synthetic", con_id=cid, bid=bid, ask=ask, bid_size=100, ask_size=100,
                             observed_at=now, data_type="synthetic", spot=float(self.mid[c.symbol][k]),
                             iv=float(self.iv[cid]), greek_at=now, model_price=p, delta=d, gamma=gm, vega=v,
                             theta=th, rho=rh)
        self.cache = {k: (now, out)}
        return out

    def close_value(self, positions):
        total = 0.0
        for i, q in positions.items():
            c = self.m.contract_map[i]
            total += q * (self.close_spot[c.symbol] if c.kind == "STK" else self.close_price[i]) * c.multiplier
        return total


class Recorder:
    def __init__(self, path):
        self.file = open(path, "w")

    def __call__(self, kind_, **values):
        self.file.write(json.dumps({"kind": kind_, **values}, default=str) + "\n")
        self.file.flush()


def parse_hms(value):
    return datetime.combine(SIM_DAY, datetime.strptime(value, "%H:%M:%S").time(), NY)


async def run(args):
    start, end = parse_hms(args.start), parse_hms(args.end)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        if f.is_file():
            f.unlink()
    history = history_file(out / "daily.csv")
    policy = json.loads(args.policy) if args.policy else {}
    execution_over = json.loads(args.execution) if args.execution else {}
    manifest = build_manifest(history, policy, execution_over)
    log = Recorder(out / "log.jsonl")
    log("manifest", policy=manifest.policy.model_dump(mode="json"), execution=manifest.execution.model_dump(mode="json"),
        session=args.session, fill=args.fill, start=args.start, end=args.end)

    if args.solver_scale != 1.0:
        # This VM is shared (load ~4x its cores): give the solver its 10 s budget in free-core terms.
        import cvxpy as cp

        import hedge.optimizer as opt_mod

        k = args.solver_scale
        opt_mod.monotonic = lambda: _time.monotonic() / k
        original_problem_solve = cp.Problem.solve

        def scaled_solve(self, *a, **kw):
            options = kw.get("highs_options")
            if options and "time_limit" in options:
                kw["highs_options"] = {**options, "time_limit": options["time_limit"] * k}
            return original_problem_solve(self, *a, **kw)

        cp.Problem.solve = scaled_solve

    sim = SimTime(start, frozen=not args.realtime)
    svc_mod.time = TimeShim(sim)
    burn_mod.time = TimeShim(sim)
    svc_mod.asyncio = AsyncioShim(sim)
    burn_mod.asyncio = AsyncioShim(sim)
    closing_mod.asyncio = AsyncioShim(sim)

    market = ReplayMarket(manifest, Path(args.market_dir) / f"{args.session}.npz", args.spread_scale)
    svc_mod.demo_quotes = lambda m, now, elapsed=0: market.quotes(now)

    store = Store(f"sqlite:///{out}/hedge.db")
    from sqlalchemy import event

    @event.listens_for(store.engine, "connect")
    def _fast_sqlite(dbapi_connection, _record):  # sim-only: no fsync per record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
        cursor.close()

    store.initialize()
    service = TradingService(manifest, store, demo=True)
    service.now = sim.now
    store.clock = sim.now
    service.execution.clock = sim.now
    service.closing_recorder.clock = sim.now
    service.close_marks.clock = sim.now

    class NullRecorder(QuoteRecorder):
        def append(self, *a, **k):
            return None

    service.recorder = NullRecorder()
    service.burner.quiet_seconds = 1.0
    sim.queue = service.events
    broker = service.broker

    quotes = market.quotes(sim.now())
    if args.state:
        state = json.loads(Path(args.state).read_text())
        broker.positions = {int(k): int(v) for k, v in state["positions"].items() if int(v)}
        broker.cash = Decimal(state["cash"])
        broker.accrued_cash = Decimal(state["accrued_cash"])
        broker.update_quotes(quotes, sim.now())
        broker.nav = broker.cash + broker.accrued_cash + sum(
            Decimal(str(quotes[i].mid)) * q * manifest.contract_map[i].multiplier for i, q in broker.positions.items()
        )
        d0 = Decimal(state["d0"])
        target_nav = state["target"]
    else:
        broker.update_quotes(quotes, sim.now())
        broker.cash -= sum(
            Decimal(str(quotes[i].mid)) * q * manifest.contract_map[i].multiplier for i, q in broker.positions.items()
        )
        nav0 = broker.nav
        growth = (Decimal(1) + manifest.scoring_annual_rate / Decimal(360)) ** manifest.accrual_days(SIM_DAY)
        d0 = (nav0 / growth).quantize(Decimal("0.000001"))
        target_nav = None
    service.ledger.import_close(manifest.day_zero, d0, "IBKR statement (simulated Day 0 close)", manifest.account)
    target = float(service.ledger.target_for(SIM_DAY))
    if target_nav is None:
        target_nav = target
    log("baseline", nav=float(broker.nav), d0=str(d0), target=target, positions=broker.positions)

    service.store.record("configuration", manifest.account, manifest, manifest.config_hash)
    service.confirm_configuration(manifest.config_hash)
    service.burner.restore_setting()
    service.restore_autostart()

    # ------------------------------------------------------------------ instrumentation
    solves = []
    original_solve = service.optimizer.solve

    def timed_solve(*a, **k):
        t0 = _time.perf_counter()
        result = original_solve(*a, **k)
        stock_only = a[5] if len(a) > 5 else k.get("stock_only", False)
        row = {"at": sim.now().isoformat(), "wall_s": round(_time.perf_counter() - t0, 3), "status": result.status,
               "stock_only": bool(stock_only), "current": result.current_score, "expected": result.expected_score,
               "quantities": {str(i): q for i, q in result.quantities.items()
                              if i not in manifest.mandatory and q}}
        solves.append(row)
        log("solve", **row)
        return result

    service.optimizer.solve = timed_solve

    if args.fill == "touch":
        original_fill = broker.fill

        async def touch_fill(order_id, quantity=None):
            if order_id in broker.package_orders or order_id not in broker.orders:
                return await original_fill(order_id, quantity)
            order = broker.orders[order_id]
            request = broker.requests.get(order_id)
            q = broker._quotes.get(order.con_id)
            if request is not None and request.order_type == "LMT" and q is not None:
                limit = float(request.limit_price)
                if order.signed_remaining > 0 and limit < q.ask - 1e-9:
                    return None
                if order.signed_remaining < 0 and limit > q.bid + 1e-9:
                    return None
            return await original_fill(order_id, quantity)

        broker.fill = touch_fill

    original_execute = broker._execute
    fills = []

    def logged_execute(order_id, con_id, signed, price, quote, order_ref):
        fee_before = broker.cash
        execution = original_execute(order_id, con_id, signed, price, quote, order_ref)
        c = manifest.contract_map[con_id]
        paid = float(fee_before - broker.cash) - float(price) * signed * c.multiplier
        row = {"at": sim.now().isoformat(), "con_id": con_id, "label": c.label, "kind": c.kind, "qty": signed,
               "price": float(price), "mid": quote.mid, "bid": quote.bid, "ask": quote.ask, "fee": round(paid, 4),
               "spread_cost": (float(price) - quote.mid) * signed * c.multiplier, "order_ref": order_ref}
        fills.append(row)
        log("fill", **row)
        return execution

    broker._execute = logged_execute
    original_submit = broker.submit

    async def logged_submit(request, order_id):
        c = manifest.contract_map[request.con_id]
        log("submit", at=sim.now().isoformat(), order_id=order_id, con_id=request.con_id, label=c.label,
            qty=request.signed_quantity, limit=str(request.limit_price), type=request.order_type,
            urgency=request.urgency, tif=request.time_in_force)
        result = await original_submit(request, order_id)
        if args.fill == "touch" and order_id in broker.orders:
            await broker.fill(order_id)  # a marketable order executes on arrival, not at the next sweep
        return result

    broker.submit = logged_submit
    original_replace = broker.replace

    async def logged_replace(order_id, limit_price):
        log("replace", at=sim.now().isoformat(), order_id=order_id, limit=limit_price)
        result = await original_replace(order_id, limit_price)
        if args.fill == "touch" and order_id in broker.orders:
            await broker.fill(order_id)
        return result

    broker.replace = logged_replace
    original_cancel = broker.cancel

    async def logged_cancel(order_id):
        log("cancel", at=sim.now().isoformat(), order_id=order_id)
        return await original_cancel(order_id)

    broker.cancel = logged_cancel

    pauses = {"busy": 0, "idle": 0}

    async def monitor_pause():
        pause = manifest.policy.risk_seconds
        if args.idle_seconds > pause:
            busy = (
                service.mode != "PAPER"
                or service.pending_target is not None
                or bool(broker.orders)
                or service.force_optimize
                or bool(service.execution.unresolved())
                or (service.last_risk is not None and not service.last_risk.passed)
            )
            if not busy:
                # Idle: sleep to the next due optimization (never past it), at most idle_seconds.
                mono = svc_mod.time.monotonic()
                due = min(service.last_optimization["STK"] + manifest.policy.stock_seconds,
                          service.last_optimization["OPT"] + manifest.policy.option_seconds) - mono
                pause = max(pause, min(args.idle_seconds, due))
            pauses["busy" if busy else "idle"] += 1
        await sim.sleep(pause, service.monitor_wakeup)
        service.monitor_wakeup.clear()

    service._monitor_pause = monitor_pause

    async def burner_wait(timeout=1.0):
        await sim.sleep(timeout, service.burner.wakeup)
        service.burner.wakeup.clear()

    service.burner.wait = burner_wait
    first_paper = {}

    operator = {"stops": [], "resumes": [], "paused_since": None, "next_try": None, "paused_seconds": 0.0}

    async def operator_step(now):
        """An attentive operator re-enables paper trading (dashboard) some time after a hard stop."""
        if service.mode != "PAUSED" or not first_paper:
            operator["paused_since"] = None
            return
        if operator["paused_since"] is None:
            operator["paused_since"] = now
            operator["next_try"] = now + timedelta(seconds=args.operator_resume_seconds)
            operator["stops"].append({"at": now.isoformat(), "health": service.health[:200]})
        operator["paused_seconds"] += 1.0
        if args.operator_resume_seconds <= 0 or now < operator["next_try"]:
            return
        if not manifest.can_trade(now, "STK"):
            return
        try:
            await service.control("paper")
            operator["resumes"].append({"at": now.isoformat(), "mode": service.mode})
            log("operator_resume", at=now.isoformat(), mode=service.mode, health=service.health[:200])
        except Exception as exc:  # noqa: BLE001 - retry like an operator would
            operator["next_try"] = now + timedelta(seconds=60)
            log("operator_resume_failed", at=now.isoformat(), error=str(exc)[:200])

    async def streamer():
        last_state = None
        last_minute = None
        while True:
            now = sim.now()
            broker.update_quotes(market.quotes(now), now)
            await operator_step(now)
            state = (service.mode, service.health[:160])
            if state != last_state:
                log("state", at=now.isoformat(), mode=service.mode, health=service.health[:300])
                last_state = state
            if service.mode == "PAPER" and "at" not in first_paper:
                first_paper["at"] = now.isoformat()
            minute = now.replace(second=0, microsecond=0)
            if minute != last_minute:
                last_minute = minute
                log("tick", at=now.isoformat(), mode=service.mode, nav=float(broker.nav),
                    positions={str(i): q for i, q in broker.positions.items() if i not in manifest.mandatory and q},
                    orders=len(broker.orders), pending=service.pending_target is not None)
            if now >= end:
                return
            await sim.sleep(1.0)

    tasks = {
        "events": asyncio.create_task(service._event_loop()),
        "monitor": asyncio.create_task(service._monitor_loop()),
        "burner": asyncio.create_task(service._burner_loop()),
        "recorder": asyncio.create_task(service.closing_recorder.run()),
        "marks": asyncio.create_task(service.close_marks.run()),
    }
    stream = asyncio.create_task(streamer())
    sim.managed = {tasks["monitor"], tasks["burner"], tasks["recorder"], tasks["marks"], stream}
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
    wall = round(_time.perf_counter() - wall0, 1)
    log("end", at=sim.now().isoformat(), wall_s=wall)

    snap = broker.snapshot()
    positions = {i: q for i, q in broker.positions.items() if q}
    close_nav = float(broker.cash + broker.accrued_cash) + market.close_value(positions)
    acct = manifest.account
    decisions = store.records("decision", acct, 100000)
    summary = {
        "session": args.session, "fill": args.fill, "policy": policy, "start": args.start, "end": args.end,
        "mode": service.mode, "health": service.health, "first_paper": first_paper.get("at"),
        "target": target_nav, "app_target": target, "d0": str(d0),
        "broker_nav_end": float(broker.nav), "close_nav_model": close_nav, "miss": close_nav - target_nav,
        "cash": str(broker.cash), "accrued_cash": str(broker.accrued_cash),
        "positions": {str(i): q for i, q in positions.items()},
        "hedges": {manifest.contract_map[i].label: q for i, q in positions.items() if i not in manifest.mandatory},
        "fills": fills, "n_solves": len(solves),
        "n_full_solves": sum(not s["stock_only"] for s in solves),
        "decisions": [
            {k: d.get(k) for k in ("at", "status", "trade_eligible", "improvement", "threshold", "optimization_scope",
                                   "nav_gap", "corrective", "estimated_cost", "current_score", "expected_score")}
            | {"quantities": {str(i): q for i, q in (d.get("quantities") or {}).items()
                              if int(i) not in manifest.mandatory and q}}
            for d in decisions
        ],
        "wall_s": wall, "pauses": pauses, "operator": operator,
    }
    (out / "summary.json").write_text(json.dumps(summary, default=str, indent=1))
    state = {"positions": {str(i): q for i, q in positions.items()}, "cash": str(broker.cash),
             "accrued_cash": str(broker.accrued_cash), "d0": str(d0), "target": target_nav,
             "nav": str(broker.nav), "at": sim.now().isoformat()}
    (out / "state.json").write_text(json.dumps(state, indent=1))
    store.close()
    print(json.dumps({"out": str(out), "mode": service.mode, "miss": round(close_nav - target_nav, 2),
                      "fills": len(fills), "solves": len(solves), "wall_s": wall}))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--start", default="09:30:00")
    p.add_argument("--end", default="16:00:00")
    p.add_argument("--policy", default=None, help="JSON policy overrides")
    p.add_argument("--execution", default=None, help="JSON execution overrides")
    p.add_argument("--fill", default="touch", choices=["touch", "mid"])
    p.add_argument("--state", default=None, help="warm-start state.json (positions/cash/d0)")
    p.add_argument("--spread-scale", type=float, default=1.0)
    p.add_argument("--realtime", action="store_true", help="let sim time run while code computes")
    p.add_argument("--market-dir", default=str(LANE / "market"))
    p.add_argument("--operator-resume-seconds", type=float, default=300.0,
                   help="re-enable paper this long after a hard stop (0 = never)")
    p.add_argument("--solver-scale", type=float, default=3.0,
                   help="multiply the solver wall budget (deadline clock and HiGHS time_limit) for CPU contention")
    p.add_argument("--idle-seconds", type=float, default=15.0,
                   help="monitor pause when nothing is pending/working (capped at the next due solve)")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()

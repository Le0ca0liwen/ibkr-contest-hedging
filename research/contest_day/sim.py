"""Contest-day simulation harness (lane contest_day).

Runs the REAL TradingService(demo=True) tasks (_event_loop, _monitor_loop, _burner_loop,
ClosingRecorder.run, CloseMarkRecorder.run) on the contest manifest shape with:
  * a discrete-event clock: sim time runs at real speed while any task computes (solver, scenario
    builds, DB writes) and jumps forward only when every loop is parked in its sleep;
  * module-level `time.monotonic` in hedge.service / hedge.burner_service mapped to sim seconds,
    so cadences (60 s stock optimization, 15 s autostart retry, ...) follow the sim clock;
  * a 1 s streaming quote feed (SPCX/RKLB/SPY spots, flat IVs, tick-rounded tight spreads).
Nothing touches IBKR or Moomoo; nothing is written to the repository.
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
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
REPO = os.environ.get("REPO") or str(RESEARCH_ROOT.parent)
sys.path.insert(0, REPO)
LANE = RESEARCH_ROOT / "contest_day" / "runs"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import hedge.burner_service as burn_mod  # noqa: E402
import hedge.closing as closing_mod  # noqa: E402
import hedge.service as svc_mod  # noqa: E402
from hedge.config import NY, Manifest  # noqa: E402
from hedge.data import QuoteRecorder  # noqa: E402
from hedge.models import Quote  # noqa: E402
from hedge.service import TradingService  # noqa: E402
from hedge.storage import Store  # noqa: E402
from hedge.valuation import ValuationEngine  # noqa: E402

SPOT = {"SPCX": 147.24, "RKLB": 51.15, "SPY": 768.51}
IV = {"SPCX": 0.57, "RKLB": 0.79, "SPY": 0.135}
STOCK_SPREAD = {"SPCX": 0.02, "RKLB": 0.02, "SPY": 0.01}
INTRADAY_VOL = {"SPCX": 0.57, "RKLB": 0.79, "SPY": 0.135}
EPOCH = datetime(2026, 10, 1, tzinfo=NY)


# ----------------------------------------------------------------------------- discrete-event clock
class SimTime:
    def __init__(self, start):
        self.sim_base = start
        self.real_base = _time.monotonic()
        self.sleepers = {}
        self.managed = set()
        self.jumped = 0.0
        self.queue = None

    def now(self):
        return self.sim_base + timedelta(seconds=_time.monotonic() - self.real_base)

    def mono(self):
        return 1e6 + (self.now() - EPOCH).total_seconds()

    async def sleep(self, seconds, event=None):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        key = object()
        task = asyncio.current_task()
        self.sleepers[key] = (self.now() + timedelta(seconds=max(0.0, float(seconds))), fut, event, task)
        try:
            await fut
        finally:
            self.sleepers.pop(key, None)

    async def run(self):
        real_sleep = asyncio.sleep
        while True:
            await real_sleep(0.0005)
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


# ----------------------------------------------------------------------------- market model
class Market:
    def __init__(self, manifest, day, path="static", seed=11):
        self.m = manifest
        self.engine = ValuationEngine(manifest)
        self.base = datetime.combine(day, datetime.min.time(), NY).replace(hour=9)
        self.path = path
        rng = np.random.default_rng(seed)
        seconds = 8 * 3600
        self.paths = {}
        for s in SPOT:
            if path == "walk":
                sigma = INTRADAY_VOL[s] / math.sqrt(252 * 23400)
                steps = rng.standard_normal(seconds) * sigma
                self.paths[s] = SPOT[s] * np.exp(np.cumsum(steps) - np.cumsum(steps)[3600 * 6 + 1800])
            else:
                self.paths[s] = None
        self.overrides = {}  # con_id -> dict(bid=..., ask=...) scripted book changes

    def spot(self, symbol, now):
        t = (now - self.base).total_seconds()
        if self.paths[symbol] is None:
            # Tiny oscillation (well under a tick of NAV-relevant movement): a quiet market.
            return SPOT[symbol] * (1 + 2e-5 * math.sin(t / 97.0))
        i = int(min(max(t, 0), len(self.paths[symbol]) - 1))
        return float(self.paths[symbol][i])

    def quotes(self, now):
        out = {}
        mids = {}
        for c in self.m.contracts:
            if c.kind != "STK":
                continue
            s = self.spot(c.symbol, now)
            spread = STOCK_SPREAD[c.symbol]
            bid = math.floor((s - spread / 2) * 100 + 1e-9) / 100
            ask = round(bid + spread, 2)
            mids[c.symbol] = (bid + ask) / 2
            out[c.con_id] = Quote(
                provider="synthetic",
                con_id=c.con_id,
                bid=bid,
                ask=ask,
                bid_size=500,
                ask_size=500,
                observed_at=now,
                data_type="synthetic",
                spot=mids[c.symbol],
                model_price=mids[c.symbol],
                shortable_shares=None,
            )
        for c in self.m.contracts:
            if c.kind != "OPT":
                continue
            spot = mids[c.symbol]
            iv = IV[c.symbol]
            price = self.engine.price(c, spot, iv, now)
            greeks = self.engine.greeks(c, spot, iv, now)
            half = min(0.05, max(0.01, 0.01 * price))
            bid = max(0.01, math.floor((price - half) * 100 + 1e-9) / 100)
            ask = math.ceil((price + half) * 100 - 1e-9) / 100
            out[c.con_id] = Quote(
                provider="synthetic",
                con_id=c.con_id,
                bid=bid,
                ask=ask,
                bid_size=100,
                ask_size=100,
                observed_at=now,
                data_type="synthetic",
                spot=spot,
                iv=iv,
                greek_at=now,
                model_price=price,
                **greeks,
            )
        for i, update in self.overrides.items():
            if i in out:
                out[i] = out[i].model_copy(update=update)
        return out


def write_history(path, end, seed=5):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=95)
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.009, len(days))
    rows = []
    for s in SPOT:
        beta = {"SPCX": 1.8, "RKLB": 2.2}.get(s, 1.0)
        idio = 0.0005 if s == "SPY" else {"SPCX": 0.040, "RKLB": 0.050}[s] * 0.7
        rets = beta * market + rng.normal(0, idio, len(days))
        px = SPOT[s] * np.exp(np.cumsum(rets) - rets.sum())
        rows += [{"date": str(d.date()), "symbol": s, "close": p} for d, p in zip(days, px, strict=True)]
    pd.DataFrame(rows).to_csv(path, index=False)


def build_manifest(burner_mode, history_path, autostart="paper", burner_over=None):
    data = yaml.safe_load((RESEARCH_ROOT / "config" / "small.contest.yaml").read_text())
    template = yaml.safe_load(Path(REPO, "config/contest.template.yaml").read_text())
    data["policy"] = {**data["policy"], "burn_surplus": False}
    burner = dict(template["burner"])
    burner["mode"] = burner_mode
    burner.update(burner_over or {})
    data["burner"] = burner
    data["autostart"] = autostart
    data["daily_stock_history"] = str(history_path)
    return Manifest.model_validate(data)


# ----------------------------------------------------------------------------- run
class Recorder:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "w")

    def __call__(self, kind_, **values):
        self.file.write(json.dumps({"kind": kind_, **values}, default=str) + "\n")
        self.file.flush()


async def run(args):
    day = date.fromisoformat(args.day)
    start = datetime.combine(day, datetime.strptime(args.start, "%H:%M:%S").time(), NY)
    end = datetime.combine(day, datetime.strptime(args.end, "%H:%M:%S").time(), NY)
    out = LANE / args.name
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        if f.is_file():
            f.unlink()
    history = out / "daily.csv"
    previous_session = (pd.Timestamp(day) - pd.offsets.BDay(1)).date()
    write_history(history, previous_session)
    over = json.loads(args.burner_over) if args.burner_over else None
    manifest = build_manifest(args.burner, history, args.autostart, over)
    log = Recorder(out / "log.jsonl")
    log("manifest", config_hash=manifest.config_hash, burner=manifest.burner.model_dump(mode="json"),
        autostart=manifest.autostart, allowlist=manifest.allowlist, mandatory=manifest.mandatory)

    sim = SimTime(start)
    svc_mod.time = TimeShim(sim)
    burn_mod.time = TimeShim(sim)
    svc_mod.asyncio = AsyncioShim(sim)
    burn_mod.asyncio = AsyncioShim(sim)
    closing_mod.asyncio = AsyncioShim(sim)

    market = Market(manifest, day, args.path)
    if args.spy_jump:
        when, frac = args.spy_jump.split(",")
        jump_at = datetime.combine(day, datetime.strptime(when, "%H:%M:%S").time(), NY)
        base_spot = market.spot

        def jumped_spot(symbol, now, base_spot=base_spot):
            value = base_spot(symbol, now)
            return value * (1 + float(frac)) if symbol == "SPY" and now >= jump_at else value

        market.spot = jumped_spot
    svc_mod.demo_quotes = lambda m, now, elapsed=0: market.quotes(now)

    store = Store(f"sqlite:///{out}/hedge.db")
    store.initialize()
    service = TradingService(manifest, store, demo=True)
    service.now = sim.now
    store.clock = sim.now
    service.execution.clock = sim.now
    service.closing_recorder.clock = sim.now
    service.close_marks.clock = sim.now
    service.recorder = QuoteRecorder(str(out / "quotes"))
    service.burner.quiet_seconds = 1.0
    sim.queue = service.events

    if args.el_frac is not None:
        raw_snapshot = service.broker.snapshot

        def generous_snapshot():
            snap = raw_snapshot()
            return snap.model_copy(update={"excess_liquidity": snap.nav * Decimal(str(args.el_frac))})

        service.broker.snapshot = generous_snapshot

    # The mandated book, bought at mids (as service.start() does in demo mode).
    quotes = market.quotes(sim.now())
    broker = service.broker
    if args.missing_leg:
        broker.positions[int(args.missing_leg)] = 0
    broker.update_quotes(quotes, sim.now())
    broker.cash -= sum(
        Decimal(str(quotes[i].mid)) * q * manifest.contract_map[i].multiplier for i, q in broker.positions.items()
    )
    nav0 = broker.nav
    growth = (Decimal(1) + manifest.scoring_annual_rate / Decimal(360)) ** (
        manifest.accrual_days(day) if day in manifest.scoring_sessions else 1
    )
    if day != manifest.day_zero:
        d0 = ((nav0 - Decimal(str(args.surplus))) / growth).quantize(Decimal("0.01"))
        service.ledger.import_close(manifest.day_zero, d0, "IBKR statement (simulated Day 0 close)", manifest.account)
        log("baseline", nav0=nav0, d0=d0, target=service.ledger.target_for(day), surplus=args.surplus)
    else:
        log("baseline", nav0=nav0, d0=None)

    # Operator confirms the configuration on the dashboard (file has confirmed: false).
    service.store.record("configuration", manifest.account, manifest, manifest.config_hash)
    service.confirm_configuration(manifest.config_hash)
    service.burner.restore_setting()
    service.restore_autostart()
    log("start", mode=service.mode, autostart_pending=service.autostart_pending, now=sim.now())

    # Instrumentation --------------------------------------------------------------------
    solves = []
    original_solve = service.optimizer.solve

    def timed_solve(*a, **k):
        t0 = _time.perf_counter()
        target = original_solve(*a, **k)
        secs = _time.perf_counter() - t0
        stock_only = a[5] if len(a) > 5 else k.get("stock_only", False)
        row = {
            "at": sim.now().isoformat(),
            "wall_s": round(secs, 3),
            "status": target.status,
            "stock_only": bool(stock_only),
            "strategy": a[6] if len(a) > 6 else k.get("strategy"),
            "recovery": bool(k.get("recovery_reference")),
            "total_ms": (target.solver_metrics or {}).get("total_ms") if hasattr(target, "solver_metrics") else None,
            "current": target.current_score,
            "expected": target.expected_score,
        }
        solves.append(row)
        log("solve", **row)
        return target

    service.optimizer.solve = timed_solve
    original_build = service.scenarios.build

    def timed_build(*a, **k):
        t0 = _time.perf_counter()
        result = original_build(*a, **k)
        log("scenario_build", at=sim.now().isoformat(), wall_s=round(_time.perf_counter() - t0, 3), n=len(result.ids))
        return result

    service.scenarios.build = timed_build
    original_submit = broker.submit

    async def logged_submit(request, order_id):
        c = manifest.contract_map[request.con_id]
        log(
            "submit",
            at=sim.now().isoformat(),
            order_id=order_id,
            con_id=request.con_id,
            label=c.label,
            sec_type=c.kind,
            qty=request.signed_quantity,
            limit=str(request.limit_price) if request.limit_price is not None else None,
            tif=request.time_in_force,
            type=request.order_type,
            urgency=request.urgency,
            intent=request.intent_id,
            burn_open=service.execution.burn_open(),
            mandated=request.con_id in manifest.mandatory,
        )
        return await original_submit(request, order_id)

    broker.submit = logged_submit
    for name in ("submit_package", "cancel"):
        if hasattr(broker, name):
            original = getattr(broker, name)

            def wrap(original=original, name=name):
                async def inner(*a, **k):
                    log(name, at=sim.now().isoformat(), args=[str(x)[:200] for x in a])
                    return await original(*a, **k)

                return inner

            setattr(broker, name, wrap())

    async def monitor_pause():
        await sim.sleep(manifest.policy.risk_seconds, service.monitor_wakeup)
        service.monitor_wakeup.clear()

    service._monitor_pause = monitor_pause

    async def burner_wait(timeout=1.0):
        await sim.sleep(timeout, service.burner.wakeup)
        service.burner.wakeup.clear()

    service.burner.wait = burner_wait

    alerts_seen = set()

    async def streamer():
        last_state = None
        restore_at = (
            datetime.combine(day, datetime.strptime(args.restore_leg_at, "%H:%M:%S").time(), NY)
            if args.restore_leg_at
            else None
        )
        while True:
            now = sim.now()
            qs = market.quotes(now)
            broker.update_quotes(qs, now)
            if args.ioc_pattern and len(broker.ioc_fill_caps) < 20:
                broker.ioc_fill_caps.extend(int(x) for x in args.ioc_pattern.split(","))
            if restore_at and now >= restore_at and args.missing_leg:
                i = int(args.missing_leg)
                if broker.positions.get(i, 0) != manifest.mandatory[i]:
                    qty = manifest.mandatory[i] - broker.positions.get(i, 0)
                    broker.positions[i] = manifest.mandatory[i]
                    broker.cash -= Decimal(str(qs[i].mid)) * qty * manifest.contract_map[i].multiplier
                    log("operator_entered_leg", at=now.isoformat(), con_id=i, qty=qty)
            b = service.burner
            snap = broker.snapshot()
            state = (service.mode, b.state_name, b.reason[:90], service.health[:120])
            last = b.last or {}
            row = {
                "at": now.isoformat(),
                "mode": service.mode,
                "health": service.health,
                "burner": b.state_name,
                "reason": b.reason,
                "nav": float(snap.nav),
                "cash": float(snap.cash),
                "positions": {manifest.contract_map[i].label: q for i, q in snap.quantities().items()
                              if i not in manifest.mandatory or q != manifest.mandatory[i]},
                "orders": len(snap.orders),
                "gap_median": (last.get("gap") or {}).get("median"),
                "needed": last.get("needed"),
                "target": last.get("target"),
                "nav_estimate": last.get("nav_estimate"),
                "engaged": b.active,
                "burn_open": service.execution.burn_open(),
                "pending_target": service.pending_target is not None,
                "data_wait": bool(service.data_wait),
                "autostart_pending": service.autostart_pending,
            }
            log("tick", **row)
            if state != last_state:
                log("state", at=now.isoformat(), mode=service.mode, burner=b.state_name, reason=b.reason,
                    health=service.health)
                last_state = state
            for a in store.records("alert", manifest.account, 20):
                key = (a.get("message"), a.get("severity"))
                if key not in alerts_seen:
                    alerts_seen.add(key)
                    log("alert", seen_at=now.isoformat(), payload=a)
            if int((now - start).total_seconds()) % 10 == 0:
                t0 = _time.perf_counter()
                try:
                    st = service.state()
                    bst = service.burner.state()
                    log("dash", at=now.isoformat(), wall_s=round(_time.perf_counter() - t0, 4),
                        mode=st.get("mode"), health=st.get("health"), burner_state=bst.get("state"),
                        burner_reason=bst.get("reason"), active=bst.get("active"), today=bst.get("today"))
                except Exception as exc:  # a dashboard failure is a finding
                    log("dash_error", at=now.isoformat(), error=f"{type(exc).__name__}: {exc}")
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
    log("end", at=sim.now().isoformat(), wall_s=round(_time.perf_counter() - wall0, 1), jumped_s=round(sim.jumped, 1))

    # Final report ------------------------------------------------------------------------
    snap = broker.snapshot()
    acct = manifest.account
    report = {
        "name": args.name,
        "mode": service.mode,
        "health": service.health,
        "burner_state": service.burner.state(),
        "ledger": service.ledger.summary(),
        "closing_capture": store.records("closing_capture", acct, 20),
        "burn_cycles": store.records("burn_cycle", acct, 1000),
        "burn_results": store.records("burn_cycle_result", acct, 1000),
        "burn_events": store.records("burn_cycle_event", acct, 5000),
        "burn_decisions": store.records("burn_decision", acct, 5000),
        "intents": store.intents(acct),
        "decisions": [
            {k: d.get(k) for k in ("at", "status", "trade_eligible", "improvement", "threshold", "blocking_reasons",
                                   "optimization_scope", "nav_gap", "closing_target", "quantities", "solver_metrics")}
            for d in store.records("decision", acct, 1000)
        ],
        "alerts": store.records("alert", acct, 200),
        "continuity": store.records("continuity", acct, 50),
        "final_positions": {manifest.contract_map[i].label: q for i, q in snap.quantities().items()},
        "final_nav": str(snap.nav),
        "solves": solves,
    }
    (out / "report.json").write_text(json.dumps(report, default=str, indent=1))
    store.close()
    print(json.dumps({"name": args.name, "final_mode": service.mode, "nav": str(snap.nav),
                      "solves": len(solves), "results": len(report["burn_results"])}, default=str))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--day", default="2026-10-22")
    p.add_argument("--start", default="15:25:00")
    p.add_argument("--end", default="16:01:00")
    p.add_argument("--burner", default="paper")
    p.add_argument("--burner-over", default=None)
    p.add_argument("--autostart", default="paper")
    p.add_argument("--surplus", type=float, default=150.0)
    p.add_argument("--path", default="static")
    p.add_argument("--missing-leg", default=None)
    p.add_argument("--restore-leg-at", default=None)
    p.add_argument("--el-frac", type=float, default=None)
    p.add_argument("--ioc-pattern", default=None)
    p.add_argument("--spy-jump", default=None, help="HH:MM:SS,fraction")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from hedge.broker import IBKRBroker, SimBroker
from hedge.burner import (
    Candidate,
    capacity_per_minute,
    cycle_seconds,
    fill_efficiency,
    gap_stats,
    hysteresis,
    jit_waiting,
    l1_improvement,
    plan_cap,
    quote_error,
    round_trip_loss,
    select,
    stock_commission,
    weighted_median,
    weighted_quantile,
)
from hedge.config import NY, BurnerPolicy, Manifest, TradingPolicy
from hedge.demo import demo_manifest, demo_quotes
from hedge.execution import ExecutionManager
from hedge.models import Contract, OrderRequest, Quote, Target
from hedge.risk import RiskGate
from hedge.service import TradingService
from hedge.storage import burn_result_id

PAPER = {
    "mode": "paper",
    "admissibility_source": "Instructor: deliberate expense through ordinary trades not disallowed (test)",
    "symbols": ["BA"],
}


def with_burner(manifest, burn_surplus=False, **burner):
    data = manifest.model_dump()
    data["policy"] = {**data["policy"], "burn_surplus": burn_surplus}
    return Manifest.model_validate({**data, "burner": {**PAPER, **burner}})


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def refresh(service):
    """What one monitor iteration provides: fresh quotes, scenarios stamped at the revision, risk."""
    now = service.now()
    qs = demo_quotes(service.manifest, now)
    service.broker.update_quotes(qs, now)
    snap = service.broker.snapshot()
    service.last_snapshot, service.last_quotes = snap, qs
    service.last_scenarios = service.scenarios.build(snap, qs, now)
    service.last_scenario_at = now
    service.last_scenario_revision = service.broker_event_revision
    service.last_risk = service.gate.validate(snap, qs, service.last_scenarios, snap.quantities(), now)


async def settle(service):
    await asyncio.sleep(0)
    await service.events.join()


async def steps(service, count=1):
    for _ in range(count):
        await service.burner.step()
        await settle(service)


def close_on(day, hour=15, minute=50, second=0):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute, second=second)


@pytest.fixture
async def burning(manifest, store):
    tasks = []

    async def start(at=None, surplus=60, app="paper", **burner):
        m = with_burner(manifest, **burner)
        service = TradingService(m, store, demo=True)
        service.burner.quiet_seconds = 0
        clock = Clock(at or close_on(m.scoring_sessions[0]))
        service.now = clock
        store.clock = clock
        await service.broker.connect()
        refresh(service)
        nav = service.broker.snapshot().nav
        service.ledger.import_close(m.day_zero, nav - Decimal(str(surplus)), "fixture", m.account)
        tasks.append(asyncio.create_task(service._event_loop()))
        if app in ("paper", "shadow"):
            await service.control(app)
        refresh(service)
        return service, clock

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def results(service):
    return service.store.records("burn_cycle_result", service.manifest.account)


def events(service):
    rows = service.store.records("burn_cycle_event", service.manifest.account)
    return [e["event"] for e in sorted(rows, key=lambda e: (e["at"], e["seq"]))]


# ---------------------------------------------------------------------------------- pure math


def test_weighted_median_quantiles_and_gap_stats():
    z = np.arange(64, dtype=float)
    w = np.ones(64) / 64
    assert weighted_median(z, w) == 31  # lower median of 64 equal weights
    assert weighted_quantile(z, w, 0.05) == 3 and weighted_quantile(z, w, 0.95) == 60
    skewed = np.array([0.1, 0.1, 0.8])
    assert weighted_median(np.array([5.0, 1.0, 9.0]), skewed) == 9
    stats = gap_stats(z, w)
    assert stats["median"] == 31 and stats["mean"] == pytest.approx(31.5)
    assert stats["score"] == pytest.approx(31.5)


def test_l1_improvement_peaks_at_the_median():
    rng = np.random.default_rng(3)
    z = 40 + 15 * rng.standard_normal(64)
    w = np.ones(64) / 64
    median = weighted_median(z, w)
    losses = np.linspace(0, 2 * median, 81)
    gains = [l1_improvement(z, w, x) for x in losses]
    best = losses[int(np.argmax(gains))]
    assert abs(best - median) <= median * 0.06
    assert l1_improvement(z, w, median / 2) > 0
    assert l1_improvement(z, w, 2.5 * median) < 0  # burning far past the median hurts
    assert l1_improvement(z, w, 0) == 0


def test_hysteresis_jit_and_cycle_time():
    assert hysteresis(26, False, 25, 5) and not hysteresis(24, False, 25, 5)
    assert hysteresis(6, True, 25, 5) and not hysteresis(5, True, 25, 5)
    assert plan_cap(30, 5) == 27.5
    capacity = capacity_per_minute(6, 20)
    assert capacity == 18
    # 0.5 * 18 $/min * (6 - 1) min = $45 of comfortable capacity: wait at 40, act at 50.
    assert jit_waiting(40, capacity, 6, 0.5, 1) and not jit_waiting(50, capacity, 6, 0.5, 1)
    assert not jit_waiting(0.01, capacity, 0.5, 0.5, 1)  # inside the reserve: always act
    # Measurements may only slow the assumption (they omit the waits between cycles).
    assert cycle_seconds(20, [5, 7]) == 20 and cycle_seconds(20, [5, 7, 9]) == 20
    assert cycle_seconds(20, [25, 30, 35]) == 30
    assert cycle_seconds(0.5, [0, 0, 0]) == 1  # never divide capacity by zero
    # IOC misses and partial fills scale capacity down once three cycles were transmitted.
    row = {"transmitted": True, "expected_loss": 4.0}
    assert fill_efficiency([{**row, "realized_loss": 0.0}] * 2) == 1
    assert fill_efficiency([{**row, "realized_loss": 2.0}] * 3) == 0.5
    assert fill_efficiency([{**row, "realized_loss": 0.0}] * 3) == 0.25
    assert fill_efficiency([{**row, "realized_loss": 5.0}] * 3) == 1


def test_round_trip_cost_model():
    policy = TradingPolicy()
    stock = Contract(con_id=1, symbol="RKLB", kind="STK")
    q = Quote(
        con_id=1, bid=51.14, ask=51.16, bid_size=500, ask_size=500, observed_at=datetime.now(NY), spot=51.15
    )
    # One share: the $1 minimum is capped at 1% of value ($0.51) on each side.
    assert stock_commission(policy, 1, 51.16) == pytest.approx(0.5116)
    assert round_trip_loss(stock, q, 1, policy) == pytest.approx(0.02 + 0.5116 + 0.5114)
    assert round_trip_loss(stock, q, 400, policy) == pytest.approx(400 * 0.02 + 2 * 2.0)
    option = Contract(
        con_id=2, symbol="RKLB", kind="OPT", multiplier=100, expiry="2026-11-27", strike=50, right="P"
    )
    oq = q.model_copy(update={"con_id": 2, "bid": 2.10, "ask": 2.15})
    assert round_trip_loss(option, oq, 3, policy) == pytest.approx(3 * 100 * 0.05 + 2 * 1.95)


def _candidate(quantity, loss, con_id=10, kind="STK"):
    return Candidate(
        con_id=con_id,
        label="BA",
        kind=kind,
        quantity=quantity,
        original=0,
        expected_loss=loss,
        bid=194.99,
        ask=195.01,
        bid_size=500,
        ask_size=500,
        multiplier=1,
        quote_at="",
    )


def test_selection_never_plans_past_the_median_and_respects_caps():
    policy = BurnerPolicy(symbols=("BA",))
    z = np.full(64, 12.0) + np.linspace(-3, 3, 64)
    w = np.ones(64) / 64
    needed = weighted_median(z, w)
    options = [_candidate(q, 2 + 0.02 * q) for q in (1, 5, 25, 100, 200, 400)]
    chosen, improvement, rejected = select(options, z, w, needed, policy)
    # cap = median - stop/2 ~ 9.5: 200 shares ($6) is the largest loss below it; 400 ($10) would overshoot.
    assert chosen.quantity == 200 and improvement > policy.minimum_improvement
    assert rejected["BA:400"] == "Would plan past the median gap"
    chosen, _, rejected = select(options, z, w, needed, policy, expense_remaining=3.0)
    assert chosen.quantity == 25 and rejected["BA:200"] == "Daily expense cap"
    chosen, _, rejected = select(
        options, z, w, needed, policy, risk_error=lambda c: "Stress" if c.quantity >= 100 else None
    )
    assert chosen.quantity == 25 and rejected["BA:100"].startswith("Temporary-inventory risk")
    chosen, _, _ = select(options, z, w, 3.0, policy)
    assert chosen is None  # below the stop band nothing is planned


# ---------------------------------------------------------------------------------- configuration


def test_burner_configuration_is_validated(manifest):
    assert manifest.burner.mode == "off"
    burner = with_burner(manifest)
    assert burner.burner_stock_id("BA") == 10
    assert burner.config_hash != manifest.config_hash
    with pytest.raises(ValidationError, match="admissibility"):
        with_burner(manifest, admissibility_source="")
    with pytest.raises(ValidationError, match="allowlisted"):
        with_burner(manifest, symbols=["XYZ"])
    with pytest.raises(ValidationError, match="2\\+ minutes"):
        with_burner(manifest, stock_entry_cutoff_minutes=3)
    with pytest.raises(ValidationError, match="2\\+ minutes"):
        with_burner(manifest, option_burn=True, option_entry_cutoff_minutes=16)
    with pytest.raises(ValidationError, match="burn_surplus"):
        with_burner(manifest, burn_surplus=True)
    with pytest.raises(ValidationError, match="jit_headroom"):
        with_burner(manifest, jit_headroom=1.5)
    # A disabled burner never constrains the ordinary policy cutoffs (no burner block at all).
    raised = {**manifest.model_dump(), "policy": {"stock_cutoff_minutes": 3, "option_cutoff_minutes": 20}}
    assert Manifest.model_validate(raised).burner.mode == "off"
    with pytest.raises(ValidationError, match="sorted"):
        with_burner(manifest, quantities=[5, 1])
    with pytest.raises(ValidationError, match="stop_threshold"):
        with_burner(manifest, start_threshold=5, stop_threshold=5)
    with pytest.raises(ValidationError, match="bias"):
        with_burner(manifest, bias=3.0)
    with pytest.raises(ValidationError, match="Exit collars"):
        with_burner(manifest, exit_collar_ticks=[2, 1])
    # Symbols are config-driven: the synthetic demo manifest burns its only stock, BA.
    demo = demo_manifest()
    assert with_burner(demo).burner_stock_id("BA") == 10


# ---------------------------------------------------------------------------------- order plumbing


def _ioc(manifest, intent, signed, price):
    return OrderRequest(
        intent_id=intent,
        con_id=10,
        signed_quantity=signed,
        limit_price=Decimal(str(price)),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="cycle",
        urgency="BURN",
        time_in_force="IOC",
    )


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_sim_ioc_full_partial_and_zero_fills_with_unique_exec_ids(manifest, quotes, now):
    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    broker.readonly = False
    qs = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    qs[10] = qs[10].model_copy(update={"bid": 194.99, "ask": 195.01, "ask_size": 300, "bid_size": 80})
    broker.update_quotes(qs, now)
    await broker.submit(_ioc(manifest, "full", 100, 195.01), 1)
    full = _drain(queue)
    assert [e for e, _ in full] == ["order", "execution", "commission", "order"]
    assert full[-1][1]["status"] == "Filled" and full[-1][1]["filled"] == 100
    assert full[1][1].price == Decimal("195.01") and full[1][1].order_ref == "hedge:full"
    # Partial: only 80 shown on the bid; the remainder is cancelled with the TRUE filled count.
    await broker.submit(_ioc(manifest, "partial", -100, 194.99), 2)
    partial = _drain(queue)
    assert partial[-1][1]["status"] == "Cancelled" and partial[-1][1]["filled"] == 80
    assert partial[1][1].signed_quantity == -80
    # Not marketable (buy limit below the ask): cancelled without a fill.
    await broker.submit(_ioc(manifest, "zero", 10, 195.00), 3)
    zero = _drain(queue)
    assert [e for e, _ in zero] == ["order", "order"] and zero[-1][1]["filled"] == 0
    # Scripted cap (rehearsal hook) and nothing ever rests for a later demo fill loop.
    broker.ioc_fill_caps = [7]
    await broker.submit(_ioc(manifest, "capped", 50, 195.01), 4)
    assert _drain(queue)[-1][1]["filled"] == 7
    assert not broker.orders and broker.positions[10] == 100 - 80 + 7
    ids = [v.exec_id for e, v in full + partial if e == "execution"]
    assert len(ids) == len(set(ids))


async def test_sim_day_partials_get_unique_exec_ids_and_cancel_reports_fills(manifest, store, quotes, now):
    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    broker.readonly = False
    broker.update_quotes({i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now)
    day = _ioc(manifest, "day", 100, 196).model_copy(update={"time_in_force": "DAY", "urgency": "NORMAL"})
    await broker.submit(day, 9)
    await broker.fill(9, 30)
    await broker.fill(9, 20)  # same clock: previously collided on one exec id
    executions = [v for e, v in _drain(queue) if e == "execution"]
    assert len({e.exec_id for e in executions}) == 2
    assert all(store.execution(e) for e in executions)
    await broker.cancel(9)
    assert _drain(queue)[-1][1] == {
        "order_id": 9,
        "order_ref": "hedge:day",
        "status": "Cancelled",
        "remaining": 0,
        "filled": 50,
    }


async def test_ibkr_ioc_leg_is_a_marketable_limit_with_hedge_reference(manifest):
    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.readonly = False
    broker.qualified = broker.reconciled = True
    broker.native = {10: SimpleNamespace(symbol="BA")}
    broker.ib.isConnected = lambda: True
    sent = []
    broker.ib.placeOrder = lambda contract, order: sent.append(order) or SimpleNamespace(order=order)
    await broker.submit(_ioc(manifest, "leg", -25, 194.98), 77)
    order = sent[0]
    assert (order.tif, order.orderType, order.action, order.totalQuantity) == ("IOC", "LMT", "SELL", 25)
    assert order.orderRef == "hedge:leg" and order.outsideRth is False and order.lmtPrice == 194.98
    assert order.account == manifest.account
    hedge = _ioc(manifest, "hedge", 5, 195).model_copy(update={"time_in_force": "DAY", "urgency": "NORMAL"})
    await broker.submit(hedge, 78)
    assert sent[1].tif == "DAY"
    with pytest.raises(ValidationError, match="IOC"):
        OrderRequest(**{**_ioc(manifest, "x", 1, 1).model_dump(), "order_type": "MKT"})


async def test_manage_never_reprices_or_cancels_burn_legs(manifest, store, quotes, now):
    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    broker.update_quotes({i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now)
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    store.record("burn_cycle", manifest.account, {"cycle_id": "c1", "session": "x"}, "c1")
    request = _ioc(manifest, "burn-leg", 100, 195.01)
    store.create_burn_leg(request, {"cycle_id": "c1", "leg": "entry", "attempt": 0})
    store.transition("burn-leg", "VALIDATED")
    store.transition("burn-leg", "SUBMITTING", order_id=5)
    store.transition("burn-leg", "ACKNOWLEDGED")
    row = store.intents(manifest.account, True)[0]
    assert (row["purpose"], row["cycle_id"], row["leg"]) == ("burn", "c1", "entry")
    manager.sent_at["burn-leg"] = now - timedelta(minutes=10)  # an ordinary order would expire
    manager.paused = False
    await manager.manage(broker.quotes(), now)
    assert store.intents(manifest.account, True)[0]["state"] == "ACKNOWLEDGED"
    assert not store.records("cancel_request", manifest.account) and not manager.paused


# ---------------------------------------------------------------------------------- integrated cycles


async def test_partial_entry_exit_sized_from_fills_closes_with_realized_loss(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [120]
    await steps(service)
    assert service.burner.state_name == "ENTRY" and service.execution.burn_open()
    cycle = service.burner.cycle
    assert cycle["quantity"] == 200 and cycle["original_quantity"] == 0
    decision = service.store.records("burn_decision", service.manifest.account)[0]
    assert decision["would_trade"] and decision["needed"] > 25
    assert cycle["expected_loss"] <= decision["plan_cap"] <= decision["gap"]["median"]
    await steps(service)
    exit_leg = next(r for r in service.store.intents(service.manifest.account) if r.get("leg") == "exit")
    assert exit_leg["signed_quantity"] == -120  # sized from confirmed fills, not the 200 planned
    await steps(service)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["reason"] == "Round trip complete"
    assert (result["entry_filled"], result["exit_filled"], result["residual"]) == (120, 120, 0)
    # 120 x $0.02 spread + two $1 minimum commissions.
    assert result["realized_loss"] == pytest.approx(4.40) and not result["fees_pending"]
    assert result["entry_vwap"] == pytest.approx(195.01) and result["exit_vwap"] == pytest.approx(194.99)
    assert not service.execution.burn_open() and 10 not in service.execution.owned_inventory_ranges
    assert service.broker.snapshot().quantities().get(10, 0) == 0
    # The short-filled entry IOC is confirmed by one reconcile before its exit is sized.
    assert events(service) == ["PLANNED", "ENTRY_SUBMITTED", "RECONCILE", "EXIT_SUBMITTED", "CLOSED"]
    quality = {
        q["intent_id"]: q
        for q in service.state()["execution_quality"]
        if q.get("purpose") == "burn" and q["filled_quantity"]
    }
    assert {q["leg"] for q in quality.values()} == {"entry", "exit"}
    assert all(q["decision_slippage_usd"] is not None for q in quality.values())
    state = service.state()["burner"]
    assert state["today"]["cycles"] == 1 and state["today"]["realized_cost"] == pytest.approx(4.40)
    assert state["last_cycles"][0]["cycle_id"] == result["cycle_id"]
    # The forecast changed with the fills: the next entry waits for a fresh scenario frame.
    await steps(service)
    assert "scenario forecast" in service.burner.reason
    refresh(service)
    await steps(service)
    assert service.burner.state_name == "ENTRY"


async def test_exit_retries_with_widening_collar(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [100, 0, 0, 100]
    await steps(service, 6)
    exits = sorted(
        (e for e in service.store.records("burn_cycle_event", service.manifest.account)),
        key=lambda e: e.get("attempt", -1),
    )
    sent = [e for e in exits if e["event"] == "EXIT_SUBMITTED"]
    assert [e["collar_ticks"] for e in sent] == [0, 1, 2]
    assert [Decimal(e["limit"]) for e in sent] == [Decimal("194.99"), Decimal("194.98"), Decimal("194.97")]
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["exit_attempts"] == 3 and result["exit_filled"] == 100


async def test_exit_failure_becomes_residual_alert_and_unblocks_hedging(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [50, 0, 0, 0, 0]
    service.force_optimize = False
    await steps(service, 8)
    result = results(service)[0]
    assert result["status"] == "RESIDUAL" and result["residual"] == 50 and result["exit_attempts"] == 4
    assert "Exit attempts exhausted" in result["reason"]
    alerts = service.store.records("alert", service.manifest.account)
    assert any("RESIDUAL" in a["message"] and a["severity"] == "error" for a in alerts)
    assert not service.execution.burn_open() and service.force_optimize
    assert service.broker.snapshot().quantities()[10] == 50  # ordinary inventory now
    assert 10 not in service.execution.owned_inventory_ranges
    await steps(service, 2)
    assert len(results(service)) == 1  # the residual is never "flattened" by a new cycle silently


async def test_paused_open_cycle_waits_then_becomes_residual_at_the_exit_deadline(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [40]
    await steps(service)
    await service.control("pause")
    await steps(service)
    assert service.burner.state_name == "EXIT_BLOCKED" and service.execution.burn_open()
    assert any(
        "No liquidation" in a["message"] for a in service.store.records("alert", service.manifest.account)
    )
    assert not any(r.get("leg") == "exit" for r in service.store.intents(service.manifest.account))
    clock.at = close_on(clock.at.date(), 15, 57, 50)  # past close - 2 min - 15 s
    await steps(service)
    result = results(service)[0]
    assert result["status"] == "RESIDUAL" and result["residual"] == 40
    assert not service.execution.burn_open()


async def test_restart_rehydrates_and_resumes_the_open_exit(burning, manifest, store):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [120]
    await steps(service)  # entry filled; the process "crashes" before any exit
    assert service.execution.burn_open()
    old = service.broker
    restarted = TradingService(service.manifest, store, demo=True)
    restarted.now = clock
    store.clock = clock
    await restarted.broker.connect()
    for name in ("positions", "cash", "nav", "accrued_cash", "_counter", "_exec_counter"):
        setattr(restarted.broker, name, getattr(old, name))
    task = asyncio.create_task(restarted._event_loop())
    try:
        refresh(restarted)
        await steps(restarted)
        assert events(restarted)[-1] == "REHYDRATED" or "REHYDRATED" in events(restarted)
        assert restarted.burner.state_name == "EXIT_BLOCKED"  # restarts are PAUSED until enabled
        await restarted.control("paper")
        refresh(restarted)
        await steps(restarted, 2)
        result = results(restarted)[0]
        assert result["status"] == "CLOSED" and result["exit_filled"] == 120
        assert restarted.broker.snapshot().quantities().get(10, 0) == 0
        assert not restarted.execution.burn_open()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "case",
    [
        "day_zero",
        "before_window",
        "at_entry_cutoff",
        "after_entry_cutoff",
        "mode_off",
        "pending_hedge_intent",
        "pending_target",
    ],
)
async def test_no_burn_activity_outside_permitted_conditions(burning, manifest, case):
    at = {
        "day_zero": close_on(manifest.day_zero),
        "before_window": close_on(manifest.scoring_sessions[0], 15, 39),
        "at_entry_cutoff": close_on(manifest.scoring_sessions[0], 15, 56),
        "after_entry_cutoff": close_on(manifest.scoring_sessions[0], 15, 56, 1),
    }.get(case)
    service, clock = await burning(at=at, mode="off" if case == "mode_off" else "paper")
    if case == "pending_hedge_intent":
        request = _ioc(manifest, "hedge-slice", 10, 195).model_copy(
            update={"time_in_force": "DAY", "urgency": "NORMAL"}
        )
        service.store.create_intent(request)
        for state in ("VALIDATED", "SUBMITTING", "ACKNOWLEDGED"):
            service.store.transition("hedge-slice", state, order_id=999 if state == "SUBMITTING" else None)
    if case == "pending_target":
        service.pending_target = Target(
            quantities={},
            expected_score=0,
            current_score=0,
            uncertainty=0,
            status="OPTIMAL",
            snapshot_id="s",
            scenario_id="x",
            config_hash="h",
        )
    await steps(service, 3)
    expected = {
        "day_zero": ("IDLE", "Day 0"),
        "before_window": ("IDLE", "window opens"),
        "at_entry_cutoff": ("DONE", "cutoff"),
        "after_entry_cutoff": ("DONE", "cutoff"),
        "mode_off": ("OFF", "disabled"),
        "pending_hedge_intent": ("WAITING", "Working orders"),
        "pending_target": ("WAITING", "Hedge plan pending"),
    }[case]
    assert service.burner.state_name == expected[0] and expected[1] in service.burner.reason
    assert not service.store.records("burn_cycle", service.manifest.account)
    assert not any(r.get("purpose") == "burn" for r in service.store.intents(service.manifest.account))
    assert not service.execution.burn_open()


@pytest.mark.parametrize("burner_mode,app", [("shadow", "paper"), ("paper", "shadow")])
async def test_shadow_records_decisions_but_sends_no_orders(burning, burner_mode, app):
    service, clock = await burning(mode=burner_mode, app=app)
    await steps(service, 3)
    assert service.burner.state_name == "SHADOW" and "Would burn" in service.burner.reason
    decisions = service.store.records("burn_decision", service.manifest.account)
    assert decisions and decisions[0]["would_trade"] and decisions[0]["mode"] == "shadow"
    assert len(decisions) == 1  # throttled: identical decisions are not re-recorded every step
    assert not service.store.intents(service.manifest.account)
    assert not service.store.records("burn_cycle", service.manifest.account)


async def test_hedge_paths_refuse_while_a_cycle_is_open(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [100]
    await steps(service)
    assert service.execution.burn_open()
    snap = service.broker.snapshot()
    target = Target(
        quantities={**snap.quantities(), 10: 0},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=service.last_scenarios.identifier,
        config_hash=service.manifest.config_hash,
    )
    result = await service.execution.submit_slice(
        target, "d", snap, service.broker.quotes(), service.last_scenarios, service.now()
    )
    assert not result["submitted"] and "Closing-burner" in result["reason"]
    service.pending_target = target
    await service._execute_pending()
    await service.optimize()
    assert "Closing-burner cycle open" in service.health
    assert service.surplus_snapshot()["execution_busy"]
    assert not [r for r in service.store.intents(service.manifest.account) if r.get("purpose") != "burn"]


async def test_unfilled_ioc_cancellations_do_not_alert_per_order(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [0]
    await steps(service, 2)
    leg = next(r for r in service.store.intents(service.manifest.account) if r.get("purpose") == "burn")
    assert leg["state"] == "CANCELLED" and results(service)[0]["reason"] == "Entry IOC unfilled"
    service.events.put_nowait(
        (
            "broker_error",
            {
                "order_id": leg["broker_order_id"],
                "code": 202,
                "message": "Order Canceled - IOC",
                "con_id": 10,
            },
        )
    )
    await settle(service)
    assert not any("IOC" in a["message"] for a in service.store.records("alert", service.manifest.account))
    assert service.mode == "PAPER"


async def test_operator_switch_only_downgrades_new_entries(burning):
    service, clock = await burning(mode="shadow")
    with pytest.raises(ValueError, match="at most"):
        service.burner.set_mode("paper")
    state = service.burner.set_mode("off")
    assert state["configured_mode"] == "off"
    await steps(service)
    assert service.burner.state_name == "OFF"
    service.burner.override = None
    service.burner.restore_setting()
    assert service.burner.override == "off"  # survives a restart of the same configuration
    assert burn_result_id("abc") == "result:abc"


async def test_restart_under_a_non_paper_configuration_hands_over_a_residual(burning, store):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [30]
    await steps(service)
    shadow = with_burner(service.manifest, mode="shadow")
    restarted = TradingService(shadow, store, demo=True)
    restarted.now = clock
    store.clock = clock
    await restarted.broker.connect()
    for name in ("positions", "cash", "nav", "accrued_cash", "_counter", "_exec_counter"):
        setattr(restarted.broker, name, getattr(service.broker, name))
    task = asyncio.create_task(restarted._event_loop())
    try:
        refresh(restarted)
        await steps(restarted)
        result = results(restarted)[0]
        assert result["status"] == "RESIDUAL" and result["residual"] == 30
        assert "not configured for paper" in result["reason"]
        assert not restarted.execution.burn_open()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_uncertain_entry_send_halts_and_is_never_resent(burning, monkeypatch):
    service, clock = await burning()
    calls = []

    async def lost(request, order_id):
        calls.append(order_id)
        raise ConnectionError("Socket dropped after placeOrder")

    monkeypatch.setattr(service.broker, "submit", lost)
    await steps(service, 3)
    assert len(calls) == 1 and service.burner.state_name == "HALTED"
    leg = next(r for r in service.store.intents(service.manifest.account) if r.get("purpose") == "burn")
    assert leg["state"] == "UNKNOWN" and service.execution.paused and service.execution.burn_open()


def test_plan_lookups_cover_legacy_and_deterministic_plan_ids(manifest, store):
    from hedge.storage import plan_record_id

    for identifier, record_id in (("legacy-plan", None), ("keyed-plan", plan_record_id("keyed-plan"))):
        request = OrderRequest(
            intent_id=identifier,
            con_id=10,
            signed_quantity=5,
            limit_price=Decimal("195"),
            account=manifest.account,
            snapshot_id="s",
            decision_id="d",
        )
        store.create_intent(request)
        store.record(
            "execution_plan",
            manifest.account,
            {"intent_id": identifier, "order_type": "LMT", "urgency": "NORMAL", "initial_limit": "195"},
            record_id,
        )
        store.transition(identifier, "VALIDATED")
        store.prepare_request(request.model_copy(update={"urgency": "DEADLINE"}), {"quote": {}})
        assert store.execution_plan(identifier, manifest.account)["urgency"] == "DEADLINE"
        assert store.intent(identifier, manifest.account)["urgency"] == "DEADLINE"
    pending = {r["id"]: r for r in store.intents(manifest.account, True)}
    assert {k: v["initial_limit"] for k, v in pending.items()} == {"legacy-plan": "195", "keyed-plan": "195"}
    assert len(store.intents(manifest.account, limit=1)) == 1


async def test_bounded_execution_quality_matches_the_full_view(burning):
    from hedge.execution_quality import execution_quality

    service, clock = await burning()
    service.broker.ioc_fill_caps = [60]
    await steps(service, 3)
    account, contracts = service.manifest.account, service.manifest.contract_map
    full = execution_quality(service.store, account, contracts)
    bounded = execution_quality(service.store, account, contracts, limit=250)
    assert full == bounded and {q["leg"] for q in full} == {"entry", "exit"}
    assert len(execution_quality(service.store, account, contracts, limit=1)) == 1


def test_burn_limits_are_exact_tick_multiples(manifest, store):
    manager = ExecutionManager(manifest, store, SimpleNamespace(price_tick=lambda *_: 0.01), None)
    book = SimpleNamespace(bid=0.29, ask=0.30)
    assert manager.burn_limit(10, book, -1, 1) == Decimal("0.28")  # not 0.27 from float error
    assert manager.burn_limit(10, book, 1) == Decimal("0.30")
    manager.broker = SimpleNamespace(price_tick=lambda *_: 0.05)
    book = SimpleNamespace(bid=1.30, ask=1.37)
    assert manager.burn_limit(10, book, 1) == Decimal("1.40")  # buy rounds up: stays marketable
    assert manager.burn_limit(10, book, -1, 2) == Decimal("1.20")


def test_burner_endpoint_is_protected_downgrade_only_and_exports_are_complete(manifest, store):
    import re

    from fastapi.testclient import TestClient

    from hedge.webapp import create_app

    service = TradingService(with_burner(manifest, mode="shadow"), store, demo=True)
    client = TestClient(create_app(service))  # no lifespan: nothing connects or trades
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    assert client.post("/api/burner", json={"mode": "off"}).status_code == 403
    response = client.post("/api/burner", json={"mode": "off"}, headers={"X-Control-Token": token})
    assert response.status_code == 200 and response.json()["configured_mode"] == "off"
    upgrade = client.post("/api/burner", json={"mode": "paper"}, headers={"X-Control-Token": token})
    assert upgrade.status_code == 409 and "at most" in upgrade.text
    bad = client.post("/api/burner", json={"mode": "max"}, headers={"X-Control-Token": token})
    assert bad.status_code == 422
    assert store.records("burner_setting", manifest.account)[0]["override"] == "off"
    for category in ("orders", "fills", "execution_quality", "burn_cycles"):
        assert client.get(f"/api/export/{category}").status_code == 200


# ---------------------------------------------------------------------------------- review regressions


def _premature_cancel(broker, which, release_after=1):
    """IB-like IOC number `which`: ib_async turns error 202 into Cancelled(filled=0) BEFORE the
    execution report and position update. The late execution arrives with download number
    `release_after` (1 = the next download)."""
    original, download = broker._submit_ioc, broker.reconcile
    late, sent, downloads = [], [], []

    def submit(request, order_id):
        sent.append(request)
        if len(sent) != which:
            return original(request, order_id)
        broker.requests[order_id] = request
        ref = "hedge:" + request.intent_id
        base = {
            "order_id": order_id,
            "perm_id": order_id,
            "order_ref": ref,
            "order_type": "LMT",
            "limit_price": float(request.limit_price),
        }
        size = request.signed_quantity
        broker.events.put_nowait(
            ("order", {**base, "status": "Submitted", "filled": 0, "remaining": abs(size)})
        )
        broker.events.put_nowait(("order", {**base, "status": "Cancelled", "filled": 0, "remaining": 0}))
        quote = broker._quotes[request.con_id]
        price = Decimal(str(quote.ask if size > 0 else quote.bid))

        def arrive():
            broker._execute(order_id, request.con_id, size, price, quote, ref)
            broker.events.put_nowait(
                ("order", {**base, "status": "Cancelled", "filled": abs(size), "remaining": 0})
            )

        late.append(arrive)
        return {"order_id": order_id, "perm_id": order_id}

    async def reconcile():
        downloads.append(1)
        while late and len(downloads) >= release_after:  # what the early status omitted
            late.pop(0)()
        return await download()

    broker._submit_ioc, broker.reconcile = submit, reconcile
    return sent


async def test_early_cancelled_exit_is_confirmed_before_another_exit_never_oversells(burning):
    service, clock = await burning()
    sent = _premature_cancel(service.broker, which=2)
    await steps(service, 5)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["reason"] == "Round trip complete"
    assert (result["entry_filled"], result["exit_filled"], result["exit_attempts"]) == (200, 200, 1)
    assert [r.signed_quantity for r in sent] == [200, -200]  # no second exit
    assert service.broker.snapshot().quantities().get(10, 0) == 0
    assert "RECONCILE" in events(service)


async def test_early_cancelled_entry_is_confirmed_before_closing_unfilled(burning):
    service, clock = await burning()
    _premature_cancel(service.broker, which=1)
    await steps(service, 4)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["reason"] == "Round trip complete"
    assert (result["entry_filled"], result["exit_filled"], result["residual"]) == (200, 200, 0)
    assert service.broker.snapshot().quantities().get(10, 0) == 0


async def test_failed_settle_reconcile_retries_instead_of_handing_over_a_residual(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [200]
    await steps(service)
    service.broker.positions[10] = 0  # the position report lags the confirmed fills
    attempts = []

    async def timeout():
        attempts.append(1)
        raise TimeoutError("reconcile timed out")

    real, service.reconcile = service.reconcile, timeout
    await steps(service)
    assert service.burner.state_name == "SETTLING"
    for _ in range(2):
        clock.at += timedelta(seconds=6)
        await steps(service)
    assert len(attempts) == 2 and not results(service) and service.execution.burn_open()
    service.reconcile, service.broker.positions[10] = real, 200
    refresh(service)
    await steps(service, 3)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["exit_filled"] == 200


async def test_burn_leg_refuses_transmission_after_its_deadline(burning):
    service, clock = await burning()
    s = service
    s.store.record("burn_cycle", s.manifest.account, {"cycle_id": "c" * 32}, "c" * 32)
    result = await s.execution.submit_burn_leg(
        cycle_id="c" * 32,
        leg="entry",
        attempt=0,
        con_id=10,
        signed=10,
        original_quantity=0,
        decision_id="c" * 32,
        snapshot=s.broker.snapshot(),
        quotes=s.broker.quotes(),
        scenarios=s.last_scenarios,
        now=clock.at,
        quote_age=3,
        deadline=clock.at,  # e.g. lock waits ran past the entry cutoff
    )
    assert not result["submitted"] and "deadline passed" in result["reason"]
    assert [r["state"] for r in s.store.burn_legs(s.manifest.account, "c" * 32)] == ["REJECTED"]
    assert not s.broker.requests


async def test_operator_downgrade_survives_configuration_edits(burning, store):
    service, clock = await burning()
    service.burner.set_mode("off")
    edited = TradingService(with_burner(service.manifest, start_threshold=30), store, demo=True)
    assert edited.manifest.config_hash != service.manifest.config_hash
    edited.burner.restore_setting()
    assert edited.burner.configured_mode() == "off"


async def test_burn_cycle_export_covers_every_session(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [50]
    await steps(service, 3)
    earlier = {**results(service)[0], "cycle_id": "e" * 32, "session": "2026-01-02"}
    service.store.record("burn_cycle_result", service.manifest.account, earlier, burn_result_id("e" * 32))
    sessions = {r["session"] for r in service.burner.export_rows()}
    assert sessions == {"2026-01-02", str(clock.at.date())}
    assert all("intent_ids" not in r for r in service.burner.export_rows())


def test_locked_books_are_never_burn_candidates(now):
    quote = Quote(
        con_id=10, bid=50, ask=50, spot=50, bid_size=500, ask_size=500, observed_at=now, data_type="live"
    )
    assert quote_error(quote, now, 3, demo=False) == "Locked book"


def test_burn_round_trips_never_touch_recovery_inventory(store, manifest, quotes, now):
    from hedge.models import Execution
    from hedge.recovery import attributed_trading_pnl
    from hedge.storage import burn_leg_id

    def fill(intent, order_id, signed, exec_id):
        store.transition(intent, "VALIDATED")
        store.transition(intent, "SUBMITTING", order_id=order_id)
        store.transition(intent, "ACKNOWLEDGED")
        store.execution(
            Execution(
                account=manifest.account,
                exec_id=exec_id,
                order_id=order_id,
                con_id=10,
                signed_quantity=signed,
                price=Decimal("195"),
                at=now,
            )
        )
        store.fee(manifest.account, exec_id, Decimal("1"))

    decision = store.record("decision", manifest.account, {"recovery": True})
    store.create_intent(_ioc(manifest, "rec-intent", 10, 195).model_copy(update={"decision_id": decision}))
    fill("rec-intent", 7, 10, "rec.01")  # recovery owns +10 BA
    cycle = "a" * 32
    store.record("burn_cycle", manifest.account, {"cycle_id": cycle}, cycle)
    for leg, signed, order_id in (("entry", 10, 8), ("exit", -10, 9)):
        request = _ioc(manifest, burn_leg_id(cycle), signed, 195).model_copy(update={"decision_id": cycle})
        store.create_burn_leg(request, {"cycle_id": cycle, "leg": leg, "attempt": 0})
        fill(request.intent_id, order_id, signed, f"burn.{order_id}")
    pnl = attributed_trading_pnl(store, manifest, quotes, now)
    assert pnl["daily_trading_pnl"] == pytest.approx(-1)  # only the recovery fill's own fee


async def test_an_exit_that_ended_short_is_confirmed_twice_so_a_late_fill_is_never_sold_again(burning):
    service, clock = await burning()
    sent = _premature_cancel(service.broker, which=2, release_after=2)
    await steps(service, 6)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and (result["exit_filled"], result["exit_attempts"]) == (200, 1)
    assert [r.signed_quantity for r in sent] == [200, -200]  # never short
    assert service.broker.snapshot().quantities().get(10, 0) == 0


async def test_a_late_fill_after_the_cycle_ended_raises_an_alert(burning):
    service, clock = await burning()
    _premature_cancel(service.broker, which=1, release_after=2)  # beyond the entry's one download
    await steps(service, 3)
    assert results(service)[0]["reason"] == "Entry IOC unfilled"
    await service.broker.reconcile()  # e.g. the routine 60 s download brings the fill
    await settle(service)
    alerts = [a["message"] for a in service.store.records("alert", service.manifest.account)]
    assert any("Late fill on a closing burner leg" in m and "+200" in m for m in alerts)


async def test_replayed_fills_of_a_finished_cycle_are_not_late_fills(burning):
    from types import SimpleNamespace

    service, clock = await burning()
    await steps(service, 4)
    assert results(service)[0]["status"] == "CLOSED"
    legs = [r for r in service.store.intents(service.manifest.account) if r.get("leg")]
    fills = service.store.fills_for_refs(service.manifest.account, ["hedge:" + r["id"] for r in legs])
    assert len(fills) >= 2
    for _ in range(3):  # every routine reconcile replays the day's executions
        for f in fills:
            service.burner.on_broker_event(
                "execution",
                SimpleNamespace(
                    order_ref=f["order_ref"], exec_id=f["exec_id"], signed_quantity=f["signed_quantity"]
                ),
            )
    alerts = [a["message"] for a in service.store.records("alert", service.manifest.account)]
    assert not any("Late fill" in m for m in alerts)


async def test_an_exit_waits_out_a_wide_book_but_sells_before_its_deadline(burning):
    service, clock = await burning()
    await steps(service)  # the entry fills

    def widen():
        refresh(service)
        quote = service.broker.quotes()[10]
        wide = quote.model_copy(update={"bid": quote.bid - 10, "observed_at": clock.at})
        service.broker.update_quotes({**service.broker.quotes(), 10: wide}, clock.at)

    widen()
    await steps(service, 2)
    assert "spread wider" in service.burner.reason and not results(service)
    exits = [r for r in service.store.intents(service.manifest.account) if r.get("leg") == "exit"]
    assert not [r for r in exits if r["broker_order_id"] is not None]
    clock.at = close_on(clock.at.date(), 15, 57, 25)  # inside the last 30 s before 15:57:45
    widen()
    await steps(service, 2)
    result = results(service)[0]
    assert result["status"] == "CLOSED" and result["exit_filled"] == 200


async def test_failed_downloads_finalize_the_cycle_at_the_close_instead_of_blocking_hedging(burning):
    service, clock = await burning()
    service.broker.ioc_fill_caps = [200]
    await steps(service)
    service.broker.positions[10] = 0  # the position report lags the confirmed fills

    async def timeout():
        raise TimeoutError("reconcile timed out")

    service.reconcile = timeout
    for _ in range(50):  # 15:50 -> past 16:00
        clock.at += timedelta(seconds=15)
        await steps(service)
    result = results(service)[0]
    assert result["status"] == "RESIDUAL" and "at the close" in result["reason"]
    assert not service.execution.burn_open()
    alerts = [a["message"] for a in service.store.records("alert", service.manifest.account)]
    assert any("RESIDUAL" in m and "broker position" in m for m in alerts)


async def test_an_uncertain_leg_from_an_earlier_session_is_closed_after_a_clean_reconcile(
    burning, monkeypatch
):
    service, clock = await burning()

    async def lost(request, order_id):
        raise ConnectionError("Socket dropped after placeOrder")

    real = service.broker.submit
    monkeypatch.setattr(service.broker, "submit", lost)
    await steps(service, 2)
    assert service.execution.unresolved()
    monkeypatch.setattr(service.broker, "submit", real)
    clock.at = close_on(service.manifest.scoring_sessions[1], 9, 45)
    refresh(service)
    await service.reconcile()
    assert not service.execution.unresolved()
    legs = [r for r in service.store.intents(service.manifest.account) if r.get("purpose") == "burn"]
    assert [r["state"] for r in legs] == ["ABANDONED"]

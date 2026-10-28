import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hedge.continuity import DataUnavailable, data_only
from hedge.demo import demo_quotes
from hedge.models import Contract, OrderRequest, Position, Target, WorkingOrder
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService
from hedge.universe import screen_options, screen_snapshot


@pytest.fixture
async def operational(manifest, store, quotes, now):
    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    service.broker.update_quotes(fresh, now)
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), fresh
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, fresh, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    task = asyncio.create_task(service._event_loop())
    await service.control("paper")
    yield service
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def risk_now(service):
    snapshot = service.broker.snapshot()
    service.last_risk = service.gate.validate(
        snapshot, service.broker.quotes(), service.last_scenarios, snapshot.quantities(), service.now()
    )
    return service.last_risk


async def test_temporary_wait_resumes_only_after_fresh_preflight(operational):
    service = operational
    original = service.broker.quotes()
    old = {
        **original,
        101: original[101].model_copy(
            update={
                "observed_at": service.now()
                - timedelta(seconds=service.manifest.risk.held_option_quote_age + 1)
            }
        ),
    }
    service.broker.update_quotes(old, service.now())
    assert not risk_now(service).passed
    await service._wait_for_data("Stale option book")
    await service._resume_after_data_wait()
    assert service.mode == "WAITING_DATA" and service.execution.paused and service.broker.readonly
    assert not service.broker.orders
    service.broker.update_quotes(original, service.now())
    assert risk_now(service).passed
    await service._resume_after_data_wait()
    assert service.mode == "PAPER" and not service.execution.paused and not service.broker.readonly
    assert service.data_wait is None and "Paused" not in service.health
    assert service.store.records("continuity", service.manifest.account)[0]["status"] == "RESUMED"


async def test_manual_pause_disarms_automatic_resume(operational):
    await operational._wait_for_data("Feed interrupted")
    await operational.control("pause")
    await operational._resume_after_data_wait()
    assert operational.mode == "PAUSED" and operational.data_wait is None
    assert operational.execution.paused and operational.broker.readonly
    assert "Operator requested pause" in operational.health


@pytest.mark.parametrize("action,profile", [("shadow", None), ("profile", "STANDARD")])
async def test_shadow_and_profile_changes_disarm_resume(operational, action, profile):
    await operational._wait_for_data("Feed interrupted")
    await operational.control(action, profile)
    await operational._resume_after_data_wait()
    assert operational.mode == "SHADOW" and operational.data_wait is None
    assert operational.execution.paused and operational.broker.readonly


async def test_configuration_change_blocks_reactivation(operational):
    await operational._wait_for_data("Feed interrupted")
    operational.manifest = operational.manifest.model_copy(update={"name": "Different configuration"})
    await operational._resume_after_data_wait()
    assert operational.mode == "PAUSED" and operational.data_wait is None
    assert "configuration change" in operational.health


async def test_no_autostart_from_paused_service(manifest, store):
    service = TradingService(manifest, store, demo=True)
    assert not service._enter_data_wait("Startup input unavailable")
    await service._resume_after_data_wait()
    assert service.mode == "PAUSED" and service.execution.paused and service.broker.readonly


async def test_unknown_submission_never_retried_or_auto_enabled(operational):
    s, m = operational, operational.manifest
    request = OrderRequest(
        intent_id="unknown",
        account=m.account,
        con_id=10,
        signed_quantity=100,
        limit_price=Decimal("195"),
        decision_id="d",
        snapshot_id=s.last_snapshot.version,
    )
    s.store.create_intent(request)
    s.store.transition(request.intent_id, "VALIDATED")
    s.store.transition(request.intent_id, "SUBMITTING", 12345)
    await s._wait_for_data("Connection interrupted")
    await s._resume_after_data_wait()
    assert s.execution.unresolved() and s.mode == "WAITING_DATA"
    assert s.broker.readonly and s.execution.paused and not s.broker.orders
    assert s.store.intents(m.account)[0]["state"] == "UNKNOWN"
    assert "uncertain orders" in s.health


async def test_cancellation_confirmed_before_reactivation(operational):
    s, m = operational, operational.manifest
    request = OrderRequest(
        intent_id="working",
        account=m.account,
        con_id=10,
        signed_quantity=10,
        limit_price=Decimal("195"),
        decision_id="d",
        snapshot_id=s.last_snapshot.version,
    )
    s.store.create_intent(request)
    s.store.transition(request.intent_id, "VALIDATED")
    oid = s.broker.reserve_order_id()
    s.store.transition(request.intent_id, "SUBMITTING", oid)
    await s.broker.submit(request, oid)
    await s.events.join()
    assert s.broker.orders
    await s._wait_for_data("Quote gap")
    await s.events.join()
    assert s.store.intents(m.account)[0]["state"] == "CANCELLED"
    await s._resume_after_data_wait()
    assert s.mode == "PAPER" and not s.broker.orders
    assert len(s.store.intents(m.account)) == 1


async def test_pause_during_automatic_reconciliation_wins(operational, monkeypatch):
    s = operational
    await s._wait_for_data("Quote gap")
    entered, release = asyncio.Event(), asyncio.Event()
    original = s.broker.reconcile

    async def delayed():
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(s.broker, "reconcile", delayed)
    resume = asyncio.create_task(s._resume_after_data_wait())
    await asyncio.wait_for(entered.wait(), 2)
    pause = asyncio.create_task(s.control("pause"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(resume, pause), 5)
    assert s.mode == "PAUSED" and s.broker.readonly and s.execution.paused
    assert s.data_wait is None and not s.broker.orders
    assert not any(r["status"] == "RESUMED" for r in s.store.records("continuity", s.manifest.account))


async def test_hard_stop_does_not_auto_resume(operational):
    await operational._wait_for_data("Quote gap")
    operational._hard_stop("Durable event processing failed")
    await operational._resume_after_data_wait()
    assert operational.mode == "PAUSED" and operational.data_wait is None
    assert operational.execution.paused and operational.broker.readonly


async def test_breached_mandate_does_not_auto_resume(operational):
    s = operational
    await s._wait_for_data("Quote gap")
    s.broker.positions[101] = 24
    await s._resume_after_data_wait()
    assert s.mode != "PAPER" and s.broker.readonly and s.execution.paused
    assert not s.broker.orders and s.broker.positions[101] == 24


async def test_missing_optional_quote_invalidates_plan_without_pausing(operational):
    s = operational
    snapshot = s.broker.snapshot()
    s.pending_target = Target(
        quantities={**snapshot.quantities(), 10: 100},
        expected_score=0,
        current_score=100,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snapshot.version,
        config_hash=s.manifest.config_hash,
        scenario_id=s.last_scenarios.identifier,
    )
    s.broker._quotes.pop(103)
    await s._execute_pending()
    assert s.mode == "PAPER" and not s.execution.paused
    assert s.pending_target is None and s.force_optimize and not s.broker.orders


async def test_pause_during_credit_preview_prevents_transmission(operational, monkeypatch):
    s, now = operational, operational.now()
    s.execution.score_context = None
    monkeypatch.setattr("hedge.models.utcnow", lambda: now)

    async def preview(request):
        s.execution.paused = True
        return SimpleNamespace(initMarginChange="0", warningText="")

    monkeypatch.setattr(s.broker, "margin_preview", preview, raising=False)
    snapshot = s.broker.snapshot()
    target = Target(
        quantities={**snapshot.quantities(), 10: 20},
        expected_score=0,
        current_score=100,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snapshot.version,
        config_hash=s.manifest.config_hash,
        scenario_id=s.last_scenarios.identifier,
    )
    result = await s.execution.submit_slice(target, "d", snapshot, s.broker.quotes(), s.last_scenarios, now)
    assert not result["submitted"] and not s.broker.orders
    assert "paused during preflight" in result["reason"]
    assert s.store.intents(s.manifest.account)[0]["state"] == "REJECTED"


@pytest.mark.parametrize(
    "reasons,expected",
    [
        (("Non-live quote: 10", "Stale account value"), True),
        (("Future account value", "Future option Greeks: 101"), True),
        (("Non-live quote: 10", "Stress loss exceeds 3% budget"), False),
        (("Mandatory conId 101: expected 25, actual 24",), False),
        ((), False),
    ],
)
def test_recoverable_fault_classification(reasons, expected):
    assert data_only(reasons) == expected


async def test_nav_attribution_lag_is_explicit_wait(operational, monkeypatch):
    monkeypatch.setattr(
        operational.nav_tracker,
        "estimate",
        lambda *a: (_ for _ in ()).throw(
            ValueError("NAV attribution waiting for matching executions or refreshed broker NAV")
        ),
    )
    with pytest.raises(DataUnavailable):
        operational._estimate_nav(operational.broker.snapshot(), operational.broker.quotes())


async def test_monitor_uses_current_quotes_after_slow_optimization(operational, monkeypatch):
    s = operational
    clock = [s.now()]
    s.now = lambda: clock[0]
    checked = asyncio.Event()
    captured = []

    async def delayed_optimizer():
        # Working orders must be managed before a potentially slow solve.
        assert captured and captured[0][1] == clock[0]
        clock[0] += timedelta(seconds=8)
        fresh = {
            i: q.model_copy(update={"observed_at": clock[0], "greek_at": clock[0]})
            for i, q in s.broker.quotes().items()
        }
        s.broker.update_quotes(fresh, clock[0])

    async def management(quotes, now):
        captured.append((quotes, now))
        if len(captured) >= 2:
            checked.set()

    monkeypatch.setattr(s, "optimize", delayed_optimizer)
    monkeypatch.setattr(s.execution, "manage", management)
    task = asyncio.create_task(s._monitor_loop())
    try:
        await asyncio.wait_for(checked.wait(), 5)
        quotes, at = captured[1]
        assert all(q.observed_at == at for q in quotes.values())
        assert s.last_quotes[10].observed_at < at - timedelta(seconds=s.manifest.risk.stock_quote_age)
        assert s.mode == "PAPER" and not s.execution.paused
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_monitor_recovers_from_missing_quote_without_operator(operational, monkeypatch):
    s = operational
    s.manifest = s.manifest.model_copy(
        update={"policy": s.manifest.policy.model_copy(update={"risk_seconds": 1})}
    )
    original_quotes = s.broker.quotes
    resumed, calls = asyncio.Event(), [0]

    def interrupted_quotes():
        calls[0] += 1
        quotes = original_quotes()
        return {i: q for i, q in quotes.items() if i != 101} if calls[0] == 1 else quotes

    original_resume = s._resume_after_data_wait

    async def recovery():
        await original_resume()
        if s.mode == "PAPER" and s.data_wait is None:
            resumed.set()

    async def no_optimizer():
        pass

    monkeypatch.setattr(s.broker, "quotes", interrupted_quotes)
    monkeypatch.setattr(s, "_resume_after_data_wait", recovery)
    monkeypatch.setattr(s, "optimize", no_optimizer)
    task = asyncio.create_task(s._monitor_loop())
    try:
        await asyncio.wait_for(resumed.wait(), 5)
        transitions = s.store.records("continuity", s.manifest.account)
        assert [t["status"] for t in reversed(transitions)] == ["WAITING_DATA", "RESUMED"]
        assert s.mode == "PAPER" and not s.execution.paused and not s.broker.readonly
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_screen_retains_working_and_closing_out_of_screen_options(manifest, snapshot, now):
    extra = tuple(
        Contract(
            con_id=1000 + j,
            symbol="BA",
            kind="OPT",
            multiplier=100,
            expiry=manifest.contract_map[101].expiry,
            strike=180 + j,
            right="C",
            underlying_id=10,
        )
        for j in range(20)
    )
    outside = Contract(
        con_id=999,
        symbol="BA",
        kind="OPT",
        multiplier=100,
        expiry=manifest.contract_map[101].expiry,
        strike=300,
        right="C",
        underlying_id=10,
    )
    m = manifest.model_copy(
        update={
            "contracts": (*manifest.contracts, *extra, outside),
            "allowlist": (*manifest.allowlist, *(c.con_id for c in extra), 999),
        }
    )
    quotes = demo_quotes(m, now)
    closing = snapshot.model_copy(
        update={
            "positions": (*snapshot.positions, Position(con_id=999, quantity=1)),
            "orders": (WorkingOrder(order_id=1, con_id=999, signed_remaining=-1),),
        }
    )
    assert 999 not in screen_options(m, quotes, closing.projected())
    assert 999 in screen_snapshot(m, quotes, closing)
    opening = snapshot.model_copy(
        update={"orders": (WorkingOrder(order_id=2, con_id=999, signed_remaining=1),)}
    )
    assert 999 in screen_snapshot(m, quotes, opening)


async def test_operator_pause_during_package_preview_prevents_submission(manifest, store, now, monkeypatch):
    from datetime import datetime

    from hedge.config import NY

    at = datetime.combine(manifest.day_zero, datetime.min.time(), NY).replace(hour=10)
    s = TradingService(manifest, store, demo=True, rehearsal=True)
    s.now = lambda: at
    await s.broker.connect()
    quotes = demo_quotes(manifest, at)
    s.broker.update_quotes(quotes, at)
    s.broker.readonly = False
    s.execution.paused = False

    async def preview(request):
        s.execution.paused = True
        return SimpleNamespace(initMarginChange="0", warningText="")

    monkeypatch.setattr(s.broker, "package_preview", preview, raising=False)
    scenarios = ScenarioEngine(manifest).build(s.broker.snapshot(), quotes, at)
    result = await s.execution.submit_package(101, 104, "d", s.broker.snapshot(), quotes, scenarios, at)
    assert not result["submitted"] and "paused during preflight" in result["reason"]
    assert not s.broker.positions and not s.broker.orders and not s.broker.package_orders
    assert all(row["state"] == "REJECTED" for row in store.intents(manifest.account))


async def test_cooldown_never_parks_a_plan_and_blocks_stock_hedging(operational, monkeypatch):
    service = operational
    clock = [5000.0]
    monkeypatch.setattr("hedge.service.time.monotonic", lambda: clock[0])
    option = next(i for i, c in service.manifest.contract_map.items() if c.kind == "OPT")
    held = service.broker.snapshot().quantities()
    service.pending_target = Target(
        quantities={**held, option: held.get(option, 0) + 1},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id="s",
        scenario_id="x",
        config_hash="h",
    )
    service.active_plan_started = False
    service.last_trade["OPT"] = clock[0] - 60  # an option plan started a minute ago
    service.last_optimization = {"STK": clock[0], "OPT": clock[0] - 60}
    await service._execute_pending()
    assert service.pending_target is None and not service.broker.orders
    assert "trade cooldown" in service.health
    clock[0] += service.manifest.policy.stock_seconds
    assert service.optimization_due()  # stock re-optimization is not blocked
    # The next full solve falls due exactly when the option cooldown ends.
    left = service.manifest.policy.option_seconds - 60
    assert service.last_optimization["OPT"] == pytest.approx(
        5000.0 - service.manifest.policy.option_seconds + left
    )


async def test_option_plan_blocked_only_by_the_stock_cooldown_is_resolved_soon(operational, monkeypatch):
    service = operational
    clock = [9000.0]
    monkeypatch.setattr("hedge.service.time.monotonic", lambda: clock[0])
    option = next(i for i, c in service.manifest.contract_map.items() if c.kind == "OPT")
    stock = next(i for i, c in service.manifest.contract_map.items() if c.kind == "STK")
    held = service.broker.snapshot().quantities()
    service.pending_target = Target(
        quantities={**held, option: held.get(option, 0) + 1, stock: held.get(stock, 0) + 10},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id="s",
        scenario_id="x",
        config_hash="h",
    )
    service.active_plan_started = False
    service.last_trade = {"OPT": 0.0, "STK": clock[0] - 20}  # only a stock trade is recent
    service.last_optimization = {"STK": clock[0], "OPT": clock[0]}
    monkeypatch.setattr(service.recovery, "paper_errors", lambda model_hash: [], raising=False)
    monkeypatch.setattr(service.recovery, "mode", "paper", raising=False)
    service.pending_recovery = {  # a recovery plan that the cooldown drops with its target
        "model_hash": None,
        "generated_at": service.now(),
        "scenarios": service.last_scenarios,
        "reference": held,
    }
    await service._execute_pending()
    assert service.pending_target is None and service.pending_recovery is None
    assert service.execution.recovery_checks is None
    assert "until" in service.health and "ET" in service.health
    # The full (option) solve is due when the 40 s left on the stock cooldown end, not in 15 min.
    clock[0] += service.manifest.policy.stock_seconds - 20
    assert clock[0] - service.last_optimization["OPT"] >= service.manifest.policy.option_seconds


async def test_reconcile_keeps_a_started_plan_after_its_own_fill_but_not_after_a_foreign_change(operational):
    service = operational
    option = next(i for i, c in service.manifest.contract_map.items() if c.kind == "OPT")
    stock = next(i for i, c in service.manifest.contract_map.items() if c.kind == "STK")
    held = service.broker.snapshot().quantities()
    plan = Target(
        quantities={**held, option: held.get(option, 0) + 3},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id="s",
        scenario_id="x",
        config_hash="h",
    )
    service.last_snapshot = service.broker.snapshot()
    service.pending_target, service.active_plan_started, service.force_optimize = plan, True, False
    service.execution.owned_inventory_ranges[option] = (held.get(option, 0), held.get(option, 0) + 1)
    service.broker.positions[option] = held.get(option, 0) + 1  # the plan's first slice filled
    await service.reconcile()
    assert service.pending_target is plan and service.active_plan_started and not service.force_optimize
    service.broker.positions[stock] = held.get(stock, 0) + 7  # a trade the app did not make
    await service.reconcile()
    assert service.pending_target is None and not service.active_plan_started and service.force_optimize


async def test_a_dropped_plan_takes_its_recovery_context_with_it(operational, monkeypatch):
    service = operational
    held = service.broker.snapshot().quantities()
    service.pending_target = Target(
        quantities=dict(held),  # nothing left to change
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id="s",
        scenario_id="x",
        config_hash="h",
    )
    monkeypatch.setattr(service.recovery, "paper_errors", lambda model_hash: [], raising=False)
    monkeypatch.setattr(service.recovery, "mode", "paper", raising=False)
    service.pending_recovery = {
        "model_hash": None,
        "generated_at": service.now(),
        "scenarios": service.last_scenarios,
        "reference": held,
    }
    await service._execute_pending()
    assert service.pending_target is None and service.pending_recovery is None


async def test_reconcile_restarts_when_no_plan_is_running_even_inside_an_old_owned_range(operational):
    service = operational
    stock = next(i for i, c in service.manifest.contract_map.items() if c.kind == "STK")
    held = service.broker.snapshot().quantities().get(stock, 0)
    service.last_snapshot = service.broker.snapshot()
    service.execution.owned_inventory_ranges[stock] = (held - 100, held + 100)  # a finished plan's
    service.pending_target, service.active_plan_started, service.force_optimize = None, False, False
    service.broker.positions[stock] = held + 50  # e.g. a trade entered by hand in TWS
    await service.reconcile()
    assert service.force_optimize


async def test_an_unchanged_book_at_reconcile_keeps_plans_and_the_option_cadence(operational):
    service = operational
    service.last_snapshot = service.broker.snapshot()
    service.pending_target, service.active_plan_started, service.force_optimize = None, False, False
    await service.reconcile()  # the routine 60 s download, nothing changed
    assert not service.force_optimize


def test_working_orders_need_not_cure_a_breach_the_market_caused(operational, monkeypatch):
    from hedge.models import RiskResult

    service = operational
    snap, quotes = service.broker.snapshot(), service.broker.quotes()
    held = snap.quantities()
    metrics = {
        "budget": 1_000_000.0,
        "stress_loss": 0.0,
        "gross_notional": 0.0,
        "residual_delta:S&P 500": 101_000.0,
        "excess_liquidity_estimate": 900_000.0,
    }
    worse = {**metrics, "residual_delta:S&P 500": 102_000.0}

    def evaluate(snapshot, quotes, scenarios, quantities, costs=0):
        result = worse if quantities.get(10, 0) != held.get(10, 0) else metrics
        return RiskResult(passed=False, reasons=("Residual delta cap: S&P 500",), metrics=result)

    monkeypatch.setattr(service.gate.engine, "evaluate", evaluate)

    def check(quantities, require_improvement):
        return service.gate.validate(
            snap,
            quotes,
            service.last_scenarios,
            quantities,
            service.now(),
            corrective=True,
            require_improvement=require_improvement,
        ).passed

    assert not check(held, True)  # an activation must improve the breach
    assert check(held, False)  # unrelated working orders leave it unchanged: no hard stop
    assert not check({**held, 10: held.get(10, 0) + 100}, False)  # but never make it worse


async def test_a_target_on_the_liquidity_boundary_passes_the_gate_that_charges_twice_its_cost(operational):
    from decimal import Decimal as D

    service = operational
    snapshot = service.broker.snapshot
    budget = min(service.optimizer.baseline_nav, float(snapshot().nav))
    floor = service.manifest.risk.liquidity_buffer * budget
    tight = None
    for slack in (50, 200, 1000, 5000, 20000):  # find an excess liquidity at which the solve binds
        service.broker.snapshot = lambda slack=slack: snapshot().model_copy(
            update={"excess_liquidity": D(str(floor + slack))}
        )
        service.last_snapshot, service.force_optimize, service.pending_target = (
            service.broker.snapshot(),
            True,
            None,
        )
        await service.optimize()
        target = service.last_target
        if target is not None and target.status == "OPTIMAL" and target.estimated_cost > 0:
            tight = target
            break
    assert tight is not None
    result = service.gate.validate(
        service.broker.snapshot(),
        service.broker.quotes(),
        service.last_scenarios,
        tight.quantities,
        service.now(),
        2 * tight.estimated_cost,
    )
    assert not any("excess liquidity" in r for r in result.reasons), result.reasons


def _breach(metrics_delta):
    from hedge.models import RiskResult

    metrics = {
        "budget": 1_000_000.0,
        "stress_loss": 0.0,
        "gross_notional": 0.0,
        "residual_delta:S&P 500": metrics_delta,
        "excess_liquidity_estimate": 900_000.0,
    }
    failed = metrics_delta > 100_000
    return RiskResult(
        passed=not failed, reasons=("Residual delta cap: S&P 500",) if failed else (), metrics=metrics
    )


def _working(service, monkeypatch, order_ref="hedge:plan-slice"):
    snap = service.broker.snapshot()
    working = snap.model_copy(
        update={"orders": (WorkingOrder(order_id=7, con_id=10, signed_remaining=100, order_ref=order_ref),)}
    )
    monkeypatch.setattr(service.broker, "snapshot", lambda: working)
    cancelled = []

    async def cancel(order_id):
        cancelled.append(order_id)

    monkeypatch.setattr(service.execution, "cancel_order", cancel)
    return cancelled


async def test_a_breach_the_market_made_after_the_risk_read_never_blames_a_working_order(
    operational, monkeypatch
):
    service = operational
    assert service.mode == "PAPER"
    service.last_risk = _breach(99_000.0)  # the loop's earlier read on older quotes passed
    monkeypatch.setattr(service.gate.engine, "evaluate", lambda *a, **k: _breach(101_000.0))
    cancelled = _working(service, monkeypatch)  # an unrelated order leaves SPY delta unchanged
    await service._check_working_orders()
    assert service.mode == "PAPER" and not cancelled


async def test_a_working_order_deepening_a_market_breach_is_withdrawn_without_stopping(
    operational, monkeypatch
):
    service = operational
    held = service.broker.snapshot().quantities()
    monkeypatch.setattr(
        service.gate.engine,
        "evaluate",
        lambda snapshot, quotes, scenarios, quantities, costs=0: _breach(
            102_000.0 if quantities.get(10, 0) != held.get(10, 0) else 101_000.0
        ),
    )
    cancelled = _working(service, monkeypatch)
    await service._check_working_orders()
    assert service.mode == "PAPER" and cancelled == [7] and service.force_optimize
    await service._check_working_orders()
    assert cancelled == [7]  # withdrawn once, not on every loop


async def test_our_order_the_market_pushed_past_a_limit_is_withdrawn_not_a_hard_stop(
    operational, monkeypatch
):
    service = operational
    held = service.broker.snapshot().quantities()
    monkeypatch.setattr(
        service.gate.engine,
        "evaluate",
        lambda snapshot, quotes, scenarios, quantities, costs=0: _breach(
            101_000.0 if quantities.get(10, 0) != held.get(10, 0) else 99_000.0
        ),
    )
    cancelled = _working(service, monkeypatch)
    await service._check_working_orders()
    assert service.mode == "PAPER" and cancelled == [7] and "withdrawn" in service.health


async def test_a_foreign_working_order_that_would_breach_a_limit_still_hard_stops(operational, monkeypatch):
    service = operational
    held = service.broker.snapshot().quantities()
    monkeypatch.setattr(
        service.gate.engine,
        "evaluate",
        lambda snapshot, quotes, scenarios, quantities, costs=0: _breach(
            101_000.0 if quantities.get(10, 0) != held.get(10, 0) else 99_000.0
        ),
    )
    cancelled = _working(service, monkeypatch, order_ref="")  # e.g. an order entered in TWS
    await service._check_working_orders()
    assert service.mode == "PAUSED" and "Working-order check" in service.health and not cancelled


def test_a_cancel_missed_while_asleep_is_settled_only_with_ibkrs_confirmation(manifest):
    import asyncio
    from types import SimpleNamespace

    from hedge.broker import IBKRBroker

    def trade(perm, status):
        return SimpleNamespace(
            order=SimpleNamespace(
                permId=perm,
                orderId=perm,
                account=manifest.account,
                orderRef="hedge:x",
                orderType="LMT",
                lmtPrice=1.0,
            ),
            orderStatus=SimpleNamespace(status=status, filled=0, remaining=100),
        )

    stale, working, unknown = trade(5, "PendingCancel"), trade(6, "Submitted"), trade(7, "PendingCancel")
    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.ib = SimpleNamespace(
        openTrades=lambda: [
            t
            for t in (stale, working, unknown)
            if t.orderStatus.status not in ("Cancelled", "Filled", "ApiCancelled", "Inactive")
        ]
    )
    # IBKR: 6 is still open (even though it also appears completed earlier), 5 is completed and not
    # open, 7 is in neither list (unknown: kept).
    broker._settle_stale_trades([trade(6, "Submitted")], [trade(5, "Cancelled"), trade(6, "Cancelled")])
    assert stale.orderStatus.status == "Cancelled" and stale.orderStatus.remaining == 0
    assert working.orderStatus.status == "Submitted" and unknown.orderStatus.status == "PendingCancel"
    assert [e[1]["perm_id"] for e in broker.events._queue] == [5]


async def test_a_sleep_gap_refreshes_from_ibkr_and_alerts_once(manifest, store, monkeypatch):
    import time as clock

    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    wall, mono = [1_000_000.0], [5_000.0]
    monkeypatch.setattr(clock, "time", lambda: wall[0])
    monkeypatch.setattr(clock, "monotonic", lambda: mono[0])
    assert service._detect_sleep() is None
    wall[0] += 30
    mono[0] += 30
    assert service._detect_sleep() is None  # an ordinary pass
    service.last_reconcile = mono[0]
    wall[0] += 900  # asleep for 15 minutes: the monotonic clock stood still
    mono[0] += 2
    slept = service._detect_sleep()
    assert slept == 898
    service._after_sleep(slept)
    assert clock.monotonic() - service.last_reconcile >= manifest.policy.reconcile_seconds
    assert service.force_optimize
    alerts = [a["message"] for a in store.records("alert", manifest.account)]
    assert len(alerts) == 1 and "asleep for about 15 min" in alerts[0]
    assert store.records("continuity", manifest.account)[0]["status"] == "SLEPT"


# ------------------------------------------- internet / IBKR outages restart paper by themselves


async def test_ibkr_link_loss_waits_and_the_link_coming_back_restarts_paper(operational):
    s = operational
    assert s.mode == "PAPER"
    s.broker.events.put_nowait(
        ("broker_error", {"order_id": -1, "code": 1100, "message": "Connectivity lost", "con_id": None})
    )
    await s.events.join()
    assert s.mode == "WAITING_DATA" and s.execution.paused and s.broker.readonly
    assert s.data_wait["resume_mode"] == "PAPER"
    s.broker.events.put_nowait(
        ("broker_error", {"order_id": -1, "code": 1102, "message": "Restored", "con_id": None})
    )
    await s.events.join()
    assert s.last_reconcile == float("-inf") and s.force_optimize
    alerts = [a["message"] for a in s.store.records("alert", s.manifest.account)]
    assert any("lost its connection" in a for a in alerts) and any("reconnected" in a for a in alerts)
    risk_now(s)
    await s._resume_after_data_wait()
    assert s.mode == "PAPER" and not s.execution.paused and not s.broker.readonly


def test_the_broker_tracks_tws_link_to_ibkr(manifest):
    from hedge.broker import IBKRBroker

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.ib.isConnected = lambda: True
    assert broker.link_up()
    broker._link(-1, 1100, "Connectivity between IBKR and Trader Workstation has been lost.", None)
    assert not broker.link_up()
    broker._link(-1, 2104, "Market data farm connection is OK", None)  # unrelated notice
    assert not broker.link_up()
    broker._link(-1, 1102, "Connectivity restored - data maintained.", None)
    assert broker.link_up()
    broker.ib.isConnected = lambda: False
    assert not broker.link_up()


async def _monitor_until(s, condition, monkeypatch, timeout=5):
    s.manifest = s.manifest.model_copy(
        update={"policy": s.manifest.policy.model_copy(update={"risk_seconds": 1})}
    )

    async def no_optimizer():
        pass

    monkeypatch.setattr(s, "optimize", no_optimizer)
    task = asyncio.create_task(s._monitor_loop())
    try:
        for _ in range(timeout * 20):
            if condition():
                return True
            await asyncio.sleep(0.05)
        return False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_an_outage_error_while_trading_waits_and_restarts_paper(operational, monkeypatch):
    """Oct 22 19:50-21:29: TWS answered 2151 'Positions info is not available yet' while IBKR was
    unreachable. That is the outage, not a fault: wait, then restart paper by itself."""
    from ib_async import RequestError

    s = operational
    original, calls, seen = s.reconcile, [0], []

    async def outage_then_back():
        calls[0] += 1
        if calls[0] <= 2:
            raise RequestError(-1, 2151, "Positions info is not available yet")
        return await original()

    monkeypatch.setattr(s, "reconcile", outage_then_back)
    s.last_reconcile = float("-inf")
    original_resume = s._resume_after_data_wait

    async def watch():
        seen.append(s.mode)
        s.last_reconcile = float("-inf") if calls[0] < 3 else s.last_reconcile
        await original_resume()

    monkeypatch.setattr(s, "_resume_after_data_wait", watch)
    assert await _monitor_until(s, lambda: calls[0] >= 3 and s.mode == "PAPER", monkeypatch)
    transitions = [t["status"] for t in reversed(s.store.records("continuity", s.manifest.account))]
    assert transitions[:1] == ["WAITING_DATA"] and "RESUMED" in transitions
    assert s.pause_reason is None or "Positions info" not in (s.pause_reason or "")


async def test_an_unexpected_error_during_a_wait_keeps_waiting_instead_of_stopping(operational, monkeypatch):
    s = operational
    await s._wait_for_data("Internet down")
    epoch = s.control_epoch

    def broken():
        raise ValueError("Configured paper account not accessible; live trading is disabled")

    monkeypatch.setattr(s.ledger, "closes", broken)
    await _monitor_until(s, lambda: "not accessible" in (s.data_wait or {}).get("reason", ""), monkeypatch)
    assert s.mode == "WAITING_DATA" and s.data_wait is not None and s.control_epoch == epoch


async def test_an_unexpected_error_while_trading_with_ibkr_reachable_still_stops(operational, monkeypatch):
    s = operational

    def broken():
        raise ValueError("Unexpected failure")

    monkeypatch.setattr(s.ledger, "closes", broken)
    await _monitor_until(s, lambda: s.mode == "PAUSED", monkeypatch)
    assert s.mode == "PAUSED" and s.data_wait is None


async def test_an_order_cut_off_by_the_outage_is_closed_after_a_clean_reconcile(operational):
    s, m = operational, operational.manifest
    created = s.now() - timedelta(minutes=3)
    s.store.clock = lambda: created
    for identifier, state in (("lost", "SUBMITTING"), ("fresh", "SUBMITTING")):
        if identifier == "fresh":
            s.store.clock = lambda: s.now() - timedelta(seconds=30)
        request = OrderRequest(
            intent_id=identifier,
            account=m.account,
            con_id=10,
            signed_quantity=100,
            limit_price=Decimal("195"),
            decision_id="d",
            snapshot_id=s.last_snapshot.version,
        )
        s.store.create_intent(request)
        s.store.transition(identifier, "VALIDATED")
        s.store.transition(identifier, state, 4242 if identifier == "lost" else 4243)
    s.store.clock = s.now
    await s._wait_for_data("Mac asleep: order sent while the socket was down")
    snapshot = s.broker.snapshot().model_copy(update={"at": s.now()})
    await s.execution.reconcile(snapshot)
    states = {r["id"]: r["state"] for r in s.store.intents(m.account)}
    # Not listed by a complete download 3 minutes on: never reached IBKR. 30 s on: still uncertain.
    assert states == {"lost": "ABANDONED", "fresh": "UNKNOWN"}
    assert any("no longer at the broker" in a["message"] for a in s.store.records("alert", m.account))

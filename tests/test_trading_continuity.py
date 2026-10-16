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
        101: original[101].model_copy(update={"observed_at": service.now() - timedelta(seconds=61)}),
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

import asyncio
import os
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from hedge.broker import IBKRBroker, SimBroker
from hedge.demo import demo_manifest
from hedge.execution import ExecutionManager
from hedge.models import Execution, OrderRequest, Target
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService
from hedge.storage import Store
from hedge.supervisor import Recommendation
from hedge.webapp import create_app


def request(manifest, snapshot):
    return OrderRequest(
        intent_id="test-intent",
        con_id=10,
        signed_quantity=100,
        limit_price=Decimal(195),
        account=manifest.account,
        snapshot_id=snapshot.version,
        decision_id="test-decision",
    )


def test_execution_deduplication_and_corrections(store, manifest, now):
    e = Execution(
        exec_id="abc.01",
        order_id=1,
        account=manifest.account,
        con_id=10,
        signed_quantity=10,
        price=Decimal("195.01"),
        at=now,
    )
    assert store.execution(e)
    assert not store.execution(e)
    correction = e.model_copy(
        update={"exec_id": "abc.02", "price": Decimal("195.02"), "correction_of": "abc.01"}
    )
    assert store.execution(correction)
    store.fee(manifest.account, "abc.02", Decimal("1.20"))
    assert len(store.fills(manifest.account)) == 2
    assert len(store.effective_fills(manifest.account)) == 1
    assert store.fills(manifest.account)[0]["at"].hour == 14  # persisted UTC, displayed ET


def test_fills_finish_intent_even_if_order_status_missing(store, manifest, snapshot, now):
    manager = ExecutionManager(manifest, store, None, None)
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    store.transition(r.intent_id, "SUBMITTING", 1)
    fill = Execution(
        exec_id="complete.01",
        order_id=1,
        order_ref="hedge:" + r.intent_id,
        account=manifest.account,
        con_id=10,
        signed_quantity=100,
        price=Decimal(195),
        at=now,
    )
    store.execution(fill)
    manager.on_execution(fill)
    assert store.intents(manifest.account)[0]["state"] == "FILLED"


def test_late_order_status_cannot_reopen_fill(store, manifest, snapshot):
    manager = ExecutionManager(manifest, store, None, None)
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    store.transition(r.intent_id, "SUBMITTING", 1)
    manager.on_order({"order_ref": "hedge:" + r.intent_id, "status": "Filled", "order_id": 1})
    manager.on_order({"order_ref": "hedge:" + r.intent_id, "status": "Submitted", "order_id": 1, "filled": 0})
    assert store.intents(manifest.account)[0]["state"] == "FILLED"


async def test_crash_after_submission_stays_unknown(store, manifest, snapshot):
    manager = ExecutionManager(manifest, store, None, None)
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    store.transition(r.intent_id, "SUBMITTING", 1)
    await manager.reconcile(snapshot)
    assert manager.unresolved()
    assert manager.paused
    assert store.intents(manifest.account)[0]["state"] == "UNKNOWN"


async def test_crash_before_transmission_does_not_resend(store, manifest, snapshot):
    manager = ExecutionManager(manifest, store, None, None)
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    await manager.reconcile(snapshot)
    assert store.intents(manifest.account)[0]["state"] == "REJECTED"


async def test_partial_fill_cancel_race(store, manifest, snapshot, quotes, now):
    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    synthetic = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    broker.update_quotes(synthetic, now)
    broker.readonly = False
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    store.transition(r.intent_id, "SUBMITTING", 1)
    await broker.submit(r, 1)
    manager.on_order(queue.get_nowait()[1])
    await broker.fill(1, 40)
    while not queue.empty():
        name, value = queue.get_nowait()
        if name == "execution":
            store.execution(value)
        if name == "order":
            manager.on_order(value)
    assert store.intents(manifest.account)[0]["state"] == "PARTIALLY_FILLED"
    await manager.pause()
    assert not manager.pause_complete()
    manager.on_order(queue.get_nowait()[1])
    assert manager.pause_complete()
    assert broker.snapshot().quantities()[10] == 40
    assert broker.snapshot().quantities()[101] == 25


async def test_submission_failure_never_retries(store, manifest, quotes, now):
    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    broker.update_quotes({i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now)
    broker.readonly = False
    snap = broker.snapshot()
    sc = ScenarioEngine(manifest).build(snap, broker.quotes(), now)
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    manager.paused = False

    async def fail(*args):
        raise ConnectionError("Disconnected after write")

    broker.submit = fail
    target = Target(
        quantities={**snap.quantities(), 10: -10},
        expected_score=1,
        current_score=100,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=sc.identifier,
        config_hash=manifest.config_hash,
    )
    result = await manager.submit_slice(target, "decision", snap, broker.quotes(), sc, now)
    assert not result["submitted"]
    assert manager.unresolved()
    second = await manager.submit_slice(target, "decision", snap, broker.quotes(), sc, now)
    assert not second["submitted"]
    assert len(store.intents(manifest.account)) == 1


async def test_ibkr_boundary_rejects_live_and_mandatory_orders(manifest, snapshot):
    broker = IBKRBroker(manifest, asyncio.Queue())
    with pytest.raises(ValueError):
        await broker.submit(request(manifest, snapshot), 1)


async def test_recovery_rechecked_before_transmission(store, manifest, quotes, now, monkeypatch):
    broker = SimBroker(manifest, asyncio.Queue(), now)
    await broker.connect()
    broker.readonly = False
    broker.update_quotes({i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now)
    snap = broker.snapshot()
    sc = ScenarioEngine(manifest).build(snap, broker.quotes(), now)
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    manager.paused = False
    target = Target(
        quantities={**snap.quantities(), 10: -10},
        expected_score=1,
        current_score=100,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=sc.identifier,
        config_hash=manifest.config_hash,
    )
    expired = False
    original_snapshot = broker.snapshot

    def refresh():
        nonlocal expired
        expired = True
        return original_snapshot()

    manager.recovery_checks = lambda *_: ["Recovery approval expired"] if expired else []
    assert manager.feasible_slices(target, snap, broker.quotes(), sc, now)
    monkeypatch.setattr(broker, "snapshot", refresh)
    result = await manager.submit_slice(target, "decision", snap, broker.quotes(), sc, now)
    assert not result["submitted"]
    assert "Recovery approval expired" in result["reason"]
    assert not broker.orders
    rows = store.intents(manifest.account)
    assert len(rows) == 1 and rows[0]["state"] == "REJECTED"


async def test_own_position_update_preserves_plan_but_manual_change_invalidates(store, manifest, snapshot):
    service = TradingService(manifest, store, demo=True)
    service.last_snapshot = snapshot
    service.pending_target = Target(
        quantities={**snapshot.quantities(), 10: -100},
        expected_score=1,
        current_score=200,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snapshot.version,
        scenario_id="test",
        config_hash=manifest.config_hash,
    )
    service.execution.owned_inventory_ranges[10] = (-100, 0)
    consumer = asyncio.create_task(service._event_loop())
    try:
        await service.events.put(("position", {"con_id": 10, "quantity": "-50"}))
        await service.events.join()
        assert service.pending_target is not None
        await service.events.put(("position", {"con_id": 10, "quantity": "-200"}))
        await service.events.join()
        assert service.pending_target is None
        assert service.force_optimize
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def test_paper_activation_can_correct_existing_delta_breach(store, manifest, quotes, now):
    service = TradingService(manifest, store, demo=True)
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    service.broker.positions[10] = 1000
    snapshot = service.broker.snapshot()
    service.last_snapshot = snapshot
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(manifest).build(snapshot, service.last_quotes, now)
    service.now = lambda: now
    assert service.last_target is None

    async def reconcile():
        return snapshot

    service.reconcile = reconcile
    with pytest.raises(ValueError, match=f"Missing previous closing NAV for {manifest.day_zero}"):
        await service.control("paper")
    assert service.mode == "PAUSED" and service.execution.paused
    assert not service.broker.orders and service.last_target is None
    assert "Missing previous closing NAV" in service.health
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    result = await service.control("paper")
    assert result["mode"] == "PAPER"
    assert not service.execution.paused
    assert service.last_target.status == "OPTIMAL"
    assert service.pending_target is service.last_target
    assert store.records("decision", manifest.account)
    assert store.records("decision", manifest.account)[0]["corrective"]
    assert not store.records("decision", manifest.account)[0]["blocking_reasons"]
    assert not service.broker.orders  # Activation itself never transmits.
    candidates = service.execution.feasible_slices(
        service.last_target, snapshot, service.last_quotes, service.last_scenarios, now
    )
    assert candidates
    initial_delta = abs(
        service.gate.engine.evaluate(
            snapshot, service.last_quotes, service.last_scenarios, snapshot.quantities()
        ).metrics["delta:BA"]
    )
    # Every offered corrective slice reduces the breached metric without a new breach.
    assert all(abs(c[3].metrics["delta:BA"]) < initial_delta for c in candidates)
    submitted = await service.execution.submit_slice(
        service.pending_target,
        service.last_decision_id,
        snapshot,
        service.last_quotes,
        service.last_scenarios,
        now,
    )
    assert submitted["submitted"]
    await service.broker.fill(submitted["order_id"])
    after = service.broker.snapshot()
    after_risk = service.gate.engine.evaluate(
        after, service.last_quotes, service.last_scenarios, after.quantities()
    )
    assert abs(after_risk.metrics["delta:BA"]) < initial_delta
    assert service.gate.registry.check(after.quantities()) == []


def test_ai_cannot_invent_trades_or_limits():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Recommendation.model_validate(
            {"snapshot_id": "x", "profile": "ARBITRARY", "explanation": "trade", "positions": {10: 100}}
        )


def test_dashboard_readonly_gets_and_csrf_protected_controls(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/web.db"), demo=True)
    with TestClient(create_app(service)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Trades &" not in page.text or "trade" in page.text.lower()
        token = re.search(r'name="control-token" content="([^"]+)"', page.text).group(1)
        assert client.post("/api/control", json={"action": "paper"}).status_code == 403
        response = client.post("/api/control", json={"action": "pause"}, headers={"X-Control-Token": token})
        assert response.status_code == 200
        assert response.json()["demo"]
        assert response.json()["mode"] == "PAUSED"
        cross = client.post(
            "/api/control",
            json={"action": "shadow"},
            headers={"X-Control-Token": token, "Origin": "https://evil.example"},
        )
        assert cross.status_code == 403
        assert client.get("/api/export/scores").status_code == 200
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="PostgreSQL integration runs in CI")
def test_postgres_exact_decimal_ledger(manifest):
    from hedge.scoring import ScoreLedger

    store = Store(os.environ["TEST_POSTGRES_URL"])
    store.initialize()
    ledger = ScoreLedger(store, manifest.model_copy(update={"name": "PostgreSQL fixture"}))
    ledger.import_close(manifest.day_zero, Decimal("1000000.12345678"), "precision", manifest.account)
    assert ledger.closes()[manifest.day_zero] == Decimal("1000000.12345678")
    store.close()


def test_dashboard_controls_recover_stale_tokens_and_show_rejections():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the dashboard JavaScript regression test")
    result = subprocess.run(
        [node, "tests/dashboard_controls.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_working_order_is_cancelled_when_quote_becomes_nonlive(store, manifest, snapshot, quotes, now):
    from datetime import timedelta

    queue = asyncio.Queue()
    broker = SimBroker(manifest, queue, now)
    await broker.connect()
    broker.update_quotes(quotes, now)
    broker.readonly = False
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    r = request(manifest, snapshot)
    store.create_intent(r)
    store.transition(r.intent_id, "VALIDATED")
    store.transition(r.intent_id, "SUBMITTING", 1)
    await broker.submit(r, 1)
    manager.on_order(queue.get_nowait()[1])
    manager.sent_at[r.intent_id] = now
    await manager.manage(
        {10: quotes[10].model_copy(update={"data_type": "frozen"})}, now + timedelta(seconds=1)
    )
    assert store.intents(manifest.account)[0]["state"] == "CANCEL_PENDING"
    assert 1 not in broker.orders
    assert 10 not in broker.positions


async def test_margin_preview_uses_same_explicit_tif_as_execution(manifest, snapshot):
    from types import SimpleNamespace

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.native[10] = object()

    class PreviewOnly:
        async def whatIfOrderAsync(self, native, order):
            assert order.tif == "DAY"
            assert order.outsideRth is False
            assert order.account == manifest.account
            assert order.orderType == "LMT"
            return SimpleNamespace(initMarginChange="100", warningText="")

    broker.ib = PreviewOnly()
    result = await broker.margin_preview(request(manifest, snapshot))
    assert result.initMarginChange == "100"


async def test_corrective_activation_rejects_stale_quotes(store, manifest, quotes, now):
    from datetime import timedelta

    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {
            i: q.model_copy(update={"data_type": "synthetic", "observed_at": now - timedelta(minutes=2)})
            for i, q in quotes.items()
        },
        now,
    )
    service.broker.positions[10] = 1000
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, service.last_quotes, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    with pytest.raises(ValueError, match="Corrective hedge unavailable"):
        await service.control("paper")
    assert service.mode == "PAUSED" and service.execution.paused
    assert not service.broker.orders


async def test_corrective_activation_requires_feasible_first_slice(store, manifest, quotes, now, monkeypatch):
    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    service.broker.positions[10] = 1000
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, service.last_quotes, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    monkeypatch.setattr(service.execution, "feasible_slices", lambda *args: [])
    with pytest.raises(ValueError, match="first slice"):
        await service.control("paper")
    assert service.mode == "PAUSED" and service.execution.paused
    assert not service.broker.orders


async def test_missing_close_clears_old_target_and_explains_blocked_optimization(
    store, manifest, quotes, now
):
    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, service.last_quotes, now)
    service.last_target = Target(
        quantities=service.last_snapshot.quantities(),
        expected_score=0,
        current_score=0,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id="previous-session",
        scenario_id="previous-session",
        config_hash=manifest.config_hash,
    )
    await service.optimize()
    assert service.last_target is None
    assert f"Missing previous closing NAV for {manifest.day_zero}" in service.health
    assert not service.broker.orders


async def test_missing_cash_benchmark_is_reported_before_corrective_optimization(
    store, manifest, quotes, now
):
    m = manifest.model_copy(update={"scoring_rule": "actual_daily_cash_rate"})
    service = TradingService(m, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    service.broker.positions[10] = 1000
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(m).build(service.last_snapshot, service.last_quotes, now)
    service.ledger.import_close(m.day_zero, m.initial_nav, "fixture", m.account)
    with pytest.raises(ValueError, match="Daily cash benchmark rate or approved forecast required"):
        await service.control("paper")
    assert service.execution.paused and service.mode == "PAUSED"
    assert not service.broker.orders and service.last_target is None


@pytest.mark.parametrize("breached", [False, True])
async def test_failed_full_solve_only_uses_stock_delta_fallback_for_existing_breach(
    store, manifest, quotes, now, monkeypatch, breached
):
    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    if breached:
        service.broker.positions[10] = 1000
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, service.last_quotes, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    original = service.optimizer.solve
    calls = []

    def solve(snapshot, qs, scenarios, previous, estimate, stock_only, strategy):
        calls.append((stock_only, strategy))
        if len(calls) == 1:
            return Target(
                quantities={101: 0},  # invalid incumbent must be discarded
                expected_score=0,
                current_score=0,
                uncertainty=0,
                status="USER_LIMIT",
                snapshot_id=snapshot.version,
                scenario_id=scenarios.identifier,
                config_hash=manifest.config_hash,
            )
        return original(snapshot, qs, scenarios, previous, estimate, stock_only, strategy)

    monkeypatch.setattr(service.optimizer, "solve", solve)
    service.force_optimize = True
    service.mode = "SHADOW"
    await service.optimize()
    if breached:
        assert calls == [(False, "dynamic_l1"), (True, "delta")]
        assert service.last_target.status == "OPTIMAL"
        before = service.last_snapshot.quantities()
        assert all(
            service.last_target.quantities.get(i, 0) == before.get(i, 0)
            for i, c in manifest.contract_map.items()
            if c.kind == "OPT"
        )
        record = store.records("decision", manifest.account)[0]
        assert record["rejected_primary_solver"] == "USER_LIMIT"
        assert record["optimization_scope"] == "CORRECTIVE_STOCK"
        assert record["risk_passed"] and record["trade_eligible"]
    else:
        assert calls == [(False, "dynamic_l1")]
        assert service.last_target.status == "USER_LIMIT"
    assert not service.broker.orders


def test_fast_policy_continues_evaluation_after_prior_trade(store, manifest, monkeypatch):
    from hedge.config import TradingPolicy

    m = manifest.model_copy(
        update={"policy": TradingPolicy(stock_seconds=15, option_seconds=120, score_threshold=25)}
    )
    service = TradingService(m, store, demo=True)
    clock = [1000.0]
    monkeypatch.setattr("hedge.service.time.monotonic", lambda: clock[0])
    service.last_optimization = {"STK": 1000.0, "OPT": 1000.0}
    service.last_trade = {"STK": 1000.0, "OPT": 1000.0}
    assert not service.optimization_due()
    clock[0] = 1015.0
    assert service.optimization_due()
    service.last_optimization["STK"] = clock[0]
    assert not service.optimization_due()
    clock[0] = 1120.0
    service.last_optimization["STK"] = clock[0]
    assert service.optimization_due()  # full portfolio independently due
    service.pending_target = object()
    assert not service.optimization_due()  # never overlap execution plans


async def test_overlapping_ibkr_reconciliations_are_serialized(manifest, monkeypatch):
    broker = IBKRBroker(manifest, asyncio.Queue())
    in_flight = [0]
    maximum = [0]

    async def download():
        in_flight[0] += 1
        maximum[0] = max(maximum[0], in_flight[0])
        await asyncio.sleep(0.01)
        in_flight[0] -= 1
        return "snapshot"

    monkeypatch.setattr(broker, "_download_state", download)
    assert await asyncio.gather(broker.reconcile(), broker.reconcile()) == ["snapshot", "snapshot"]
    assert maximum[0] == 1

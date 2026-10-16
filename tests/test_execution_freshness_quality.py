import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from hedge.broker import SimBroker
from hedge.execution import ExecutionManager
from hedge.execution_quality import execution_quality
from hedge.models import Execution, OrderRequest, SetupPackageRequest, Target
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService
from hedge.storage import Store
from hedge.webapp import create_app


@pytest.fixture
async def frame(manifest, store, quotes, now):
    broker = SimBroker(manifest, asyncio.Queue(), now)
    await broker.connect()
    broker.readonly = False
    qs = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    broker.update_quotes(qs, now)
    snap = broker.snapshot()
    scenarios = ScenarioEngine(manifest).build(snap, qs, now)
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    manager.paused = False
    target = Target(
        quantities={**snap.quantities(), 10: -10},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=scenarios.identifier,
        config_hash=manifest.config_hash,
    )
    return manager, broker, snap, qs, scenarios, target


@pytest.mark.parametrize("moving_during_build", [False, "price", "inventory", "pause"])
async def test_transmission_rebuilds_changed_iv_and_rejects_moving_frame(
    frame, manifest, store, now, moving_during_build
):
    manager, broker, snap, qs, old, target = frame
    fresh = {**qs, 101: qs[101].model_copy(update={"iv": qs[101].iv + 0.001})}
    broker.update_quotes(fresh, now)
    calls = []
    builder = manager.scenario_builder

    def build(snapshot, quotes, at, ids):
        calls.append((quotes[101].iv, ids))
        result = builder(snapshot, quotes, at, ids)
        if moving_during_build == "price":
            broker._quotes = {**fresh, 101: fresh[101].model_copy(update={"iv": fresh[101].iv + 0.001})}
        elif moving_during_build == "inventory":
            broker.positions[10] = 1
        elif moving_during_build == "pause":
            manager.paused = True
        return result

    manager.scenario_builder = build
    outcome = await manager.submit_slice(target, "decision", snap, qs, old, now)
    assert calls == [(fresh[101].iv, old.ids)]
    if moving_during_build:
        assert not outcome["submitted"] and not broker.orders
        reason = {
            "price": "Market changed during scenario refresh",
            "inventory": "Inventory/orders changed",
            "pause": "paused during preflight",
        }
        assert reason[moving_during_build] in outcome["reason"]
        assert store.intents(manifest.account)[0]["state"] == "REJECTED"
    else:
        assert outcome["submitted"]
        assert manager.current_scenarios.identifier != old.identifier
        assert manager.current_scenarios.is_current(fresh, now, 5)
        record = store.records("pretransmission", manifest.account)[0]
        assert record["scenario_id"] == manager.current_scenarios.identifier


async def test_expired_frames_refresh_but_recovery_forecasts_require_replan(frame, now):
    manager, broker, snap, qs, scenarios, _ = frame
    later = now + timedelta(seconds=6)
    fresh = {i: q.model_copy(update={"observed_at": later, "greek_at": later}) for i, q in qs.items()}
    broker.update_quotes(fresh, later)
    _, _, clock, rebuilt = await manager.refresh_scenarios(scenarios, later)
    assert rebuilt.built_at == clock == later
    assert not scenarios.is_current(fresh, later, 5)
    recovery = replace(scenarios, source=scenarios.source + "; recovery stock conservative close forecast")
    with pytest.raises(ValueError, match="Recovery scenario inputs changed"):
        await manager.refresh_scenarios(recovery, later)


@pytest.mark.parametrize("sign,con_id", [(1, 10), (-1, 10), (1, 101)])
def test_quality_handles_units_direction_corrections_and_late_fees(store, manifest, now, sign, con_id):
    clock = [now]
    store.clock = lambda: clock[0]
    request = OrderRequest(
        intent_id="quality-order",
        con_id=con_id,
        signed_quantity=sign * 10,
        limit_price=Decimal("100.5"),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="decision",
    )
    store.record("decision", manifest.account, {"reference_midpoints": {str(con_id): 100.25}}, "decision")
    store.create_intent(request)
    store.record("execution_plan", manifest.account, {"intent_id": request.intent_id, "arrival_mid": 100.5})
    store.transition(request.intent_id, "VALIDATED")
    store.transition(request.intent_id, "SUBMITTING", 17)
    store.transition(request.intent_id, "ACKNOWLEDGED")
    first = Execution(
        exec_id="a.01",
        order_id=17,
        order_ref="hedge:quality-order",
        account=manifest.account,
        con_id=con_id,
        signed_quantity=sign * 4,
        price=Decimal("100.6"),
        at=now + timedelta(seconds=2),
    )
    second = first.model_copy(
        update={
            "exec_id": "b.01",
            "signed_quantity": sign * 6,
            "price": Decimal("100.4"),
            "at": now + timedelta(seconds=4),
        }
    )
    assert store.execution(first) and not store.execution(first)
    store.execution(second)
    for execution in (first, second):
        store.fee(manifest.account, execution.exec_id, Decimal("1"))
    store.amend_limit(request.intent_id, Decimal("100.6"), {})
    store.record("cancel_request", manifest.account, {"order_id": 17, "intent_ids": [request.intent_id]})
    # Reused broker ID belonging to another intent must not inflate counts.
    store.record("cancel_request", manifest.account, {"order_id": 17, "intent_ids": ["another-order"]})
    unit = manifest.contract_map[con_id].multiplier * sign
    row = execution_quality(store, manifest.account, manifest.contract_map)[0]
    assert row["filled_quantity"] == 10 and row["fill_ratio"] == 1
    assert row["average_fill_price"] == Decimal("100.48")
    assert row["decision_slippage_usd"] == Decimal("2.3") * unit
    assert row["arrival_slippage_usd"] == Decimal("-.2") * unit
    assert row["decision_cost_usd"] == Decimal("2.3") * unit + 2
    assert row["first_fill_seconds"] == 2 and row["weighted_fill_seconds"] == 3.2
    assert row["amendment_attempts"] == row["cancel_attempts"] == 1
    correction = first.model_copy(
        update={"exec_id": "a.02", "correction_of": "a.01", "price": Decimal("100.7")}
    )
    store.execution(correction)
    row = execution_quality(store, manifest.account, manifest.contract_map)[0]
    assert row["filled_quantity"] == 10
    assert not row["fees_complete"] and row["decision_cost_usd"] is None
    store.fee(manifest.account, "a.02", Decimal(".5"))
    restored = Store(str(store.engine.url))
    try:
        row = execution_quality(restored, manifest.account, manifest.contract_map)[0]
        assert row["decision_cost_usd"] == Decimal("2.7") * unit + Decimal("1.5")
    finally:
        restored.close()


def test_unknown_metrics_do_not_fabricate_cost_or_fill_and_export_works(store, manifest, snapshot, now):
    store.clock = lambda: now
    request = OrderRequest(
        intent_id="legacy",
        con_id=10,
        signed_quantity=10,
        limit_price=Decimal(195),
        account=manifest.account,
        snapshot_id=snapshot.version,
        decision_id="missing",
    )
    store.create_intent(request)
    row = execution_quality(store, manifest.account, manifest.contract_map)[0]
    assert row["fill_ratio"] is None and row["decision_mid"] is None
    assert row["decision_cost_usd"] is None and row["first_fill_seconds"] is None
    service = TradingService(manifest, store, demo=True)
    client = TestClient(create_app(service))
    response = client.get("/api/export/execution_quality")
    assert response.status_code == 200
    assert "decision_slippage_usd" in response.text and "legacy" in response.text
    assert client.get("/api/state").json()["execution_quality"][0]["filled_quantity"] == 0


async def test_cancel_attempt_is_recorded_without_claiming_a_fill(frame, store, manifest, now):
    manager, broker, snap, qs, scenarios, target = frame
    store.clock = lambda: now
    result = await manager.submit_slice(target, "decision", snap, qs, scenarios, now)
    assert result["submitted"]
    while not broker.events.empty():
        event, value = broker.events.get_nowait()
        if event == "order":
            manager.on_order(value)
    await manager.pause()
    row = execution_quality(store, manifest.account, manifest.contract_map)[0]
    assert row["cancel_attempts"] == 1 and row["fill_ratio"] == 0
    assert row["state"] == "CANCEL_PENDING" and row["first_fill_seconds"] is None
    assert row["decision_cost_usd"] is None


def test_package_metrics_count_legs_and_do_not_double_charge_net_report(store, manifest, quotes, now):
    store.clock = lambda: now
    request = SetupPackageRequest(
        package_id="combo",
        buy_id=101,
        sell_id=104,
        quantity=1,
        limit_price=Decimal(1),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="missing",
    )
    store.create_package(request, quotes)
    for i, sign, currency in [(101, 1, "USD"), (104, -1, "EUR")]:
        identifier = f"combo:{i}"
        store.transition(identifier, "VALIDATED")
        store.transition(identifier, "SUBMITTING", 9)
        fill = Execution(
            exec_id=str(i),
            order_id=9,
            order_ref="hedge-package:combo",
            account=manifest.account,
            con_id=i,
            signed_quantity=sign,
            price=Decimal(str(quotes[i].mid)) + Decimal(".01") * sign,
            at=now + timedelta(seconds=3),
        )
        store.execution(fill)
        store.fee(manifest.account, str(i), 1, currency)
    store.record("package_commission", manifest.account, {"amount": 2, "currency": "USD"})
    store.record("package_amendment", manifest.account, {"package_id": "combo"})
    rows = {r["con_id"]: r for r in execution_quality(store, manifest.account, manifest.contract_map)}
    for row in rows.values():
        assert row["amendment_attempts"] == 1
        # Stored fill prices have 8 decimals; options multiply rounding by 100.
        assert abs(row["arrival_slippage_usd"] - Decimal(1)) <= Decimal("0.000001")
    assert rows[101]["known_fees_usd"] == 1 and rows[101]["fees_complete"]
    assert rows[104]["known_fees_usd"] == 0 and not rows[104]["fees_complete"]

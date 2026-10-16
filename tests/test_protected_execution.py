from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hedge.broker import IBKRBroker
from hedge.config import NY
from hedge.demo import demo_quotes
from hedge.execution import ExecutionManager
from hedge.models import OrderRequest, SetupPackageRequest, Target
from hedge.order_policy import choose_order
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService


def at_day_zero(manifest, hour=10, minute=0):
    return datetime.combine(manifest.day_zero, datetime.min.time(), NY).replace(hour=hour, minute=minute)


async def setup_service(manifest, store, hour=10, minute=0):
    service = TradingService(manifest, store, demo=True, rehearsal=True)
    now = at_day_zero(manifest, hour, minute)
    service.now = lambda: now
    await service.broker.connect()
    qs = demo_quotes(manifest, now)
    service.broker.update_quotes(qs, now)
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = qs
    service.last_scenarios = ScenarioEngine(manifest).build(service.last_snapshot, qs, now)
    return service, now


def test_market_orders_require_urgent_fresh_deep_stock_book(manifest, quotes, now):
    q = quotes[10].model_copy(
        update={"bid": 190, "ask": 190.01, "bid_size": 1000, "ask_size": 1000, "spot": 190.005}
    )
    c = manifest.contract_map[10]
    broker = SimpleNamespace(price_tick=lambda *_: 0.01)
    assert choose_order(manifest, broker, c, q, 100, now)["order_type"] == "LMT"
    assert choose_order(manifest, broker, c, q, 100, now, corrective=True)["order_type"] == "MKT"
    assert choose_order(manifest, broker, c, q, 100, now, corrective=True, setup=True)["order_type"] == "LMT"
    assert (
        choose_order(manifest, broker, c, q.model_copy(update={"ask_size": 100}), 100, now, corrective=True)[
            "order_type"
        ]
        == "LMT"
    )
    assert (
        choose_order(manifest, broker, c, q, 100, now + timedelta(seconds=2), corrective=True)["order_type"]
        == "LMT"
    )
    assert (
        choose_order(
            manifest, broker, c, q.model_copy(update={"data_type": "delayed"}), 100, now, corrective=True
        )["order_type"]
        == "LMT"
    )
    assert (
        choose_order(manifest, broker, manifest.contract_map[101], quotes[101], 1, now, corrective=True)[
            "order_type"
        ]
        == "LMT"
    )


def test_tick_rounding_never_crosses_price_budget(manifest, quotes, now):
    q = quotes[10].model_copy(update={"bid": 100.04, "ask": 100.07, "spot": 100.055})
    broker = SimpleNamespace(price_tick=lambda *_: 0.05)
    assert choose_order(manifest, broker, manifest.contract_map[10], q, 1, now)["price"] == Decimal("100.05")
    assert choose_order(manifest, broker, manifest.contract_map[10], q, -1, now)["price"] == Decimal("100.05")
    q = q.model_copy(update={"bid": 100.01, "ask": 100.03})
    with pytest.raises(ValueError, match="valid price tick"):
        choose_order(manifest, broker, manifest.contract_map[10], q, 1, now)


def test_wide_opening_option_spread_blocked(manifest, quotes, now):
    q = quotes[102].model_copy(update={"bid": 7.25, "ask": 10.20})
    with pytest.raises(ValueError, match="spread too wide"):
        choose_order(manifest, SimpleNamespace(), manifest.contract_map[102], q, 1, now, setup=True)


async def test_day_zero_has_patient_full_session_window(manifest, store):
    service, now = await setup_service(manifest, store, 9, 31)
    with pytest.raises(ValueError, match="stabilization"):
        await service.control("setup")
    assert not store.intents(manifest.account) and not service.broker.orders
    late = at_day_zero(manifest, 15, 50)
    assert not manifest.can_trade(late, "OPT")
    assert manifest.can_trade(late, "OPT", setup=True)
    assert not manifest.can_trade(at_day_zero(manifest, 15, 59), "OPT", setup=True)


async def test_every_setup_mandatory_fill_has_matched_hedge_and_complete_plan(manifest, store):
    service, now = await setup_service(manifest, store)
    await service.control("setup")
    assert store.records("setup_plan", manifest.account)
    assert set(service.setup_pairs) == set(manifest.mandatory)
    assert all(
        service.setup_target_quantities[h] == -manifest.mandatory[i] for i, h in service.setup_pairs.items()
    )
    for _ in range(100):
        await service._setup_slice()
        for oid in list(service.broker.orders):
            if oid in service.broker.package_orders:
                assert store.get_record(
                    service.broker.package_orders[oid].package_id, manifest.account, "setup_package"
                )
            await service.broker.fill(oid)
            held = service.broker.snapshot().quantities()
            assert all(held.get(h, 0) <= -held.get(i, 0) for i, h in service.setup_pairs.items())
        while not service.events.empty():
            name, value = service.events.get_nowait()
            if name == "execution":
                store.execution(value)
                service.execution.on_execution(value)
            elif name == "order":
                service.execution.on_order(value)
            elif name == "commission":
                store.fee(manifest.account, value["exec_id"], value["amount"], value["currency"])
            service.events.task_done()
        if service.mode == "SHADOW":
            break
    assert service.mode == "SHADOW"
    assert not service.gate.registry.check(service.broker.snapshot().quantities())
    assert not store.intents(manifest.account, True)


async def test_missing_protective_hedges_cannot_fall_back_to_bare_purchase(manifest, store):
    manifest = manifest.model_copy(update={"allowlist": (10,)})
    service, now = await setup_service(manifest, store)
    with pytest.raises(ValueError, match="protective vertical"):
        await service.control("setup")
    assert not service.broker.orders and not store.intents(manifest.account)
    request = OrderRequest(
        intent_id="bare",
        con_id=101,
        signed_quantity=1,
        limit_price=Decimal(1),
        account=manifest.account,
        snapshot_id=service.broker.snapshot().version,
        decision_id="bad",
    )
    with pytest.raises(ValueError, match="Bare mandatory"):
        await service.broker.setup_mandate(request, service.broker.reserve_order_id(), now)


async def test_unknown_package_submission_is_never_resent(manifest, store, monkeypatch):
    service, now = await setup_service(manifest, store)
    await service.control("setup")
    calls = []

    async def fail(*args):
        calls.append(args)
        raise ConnectionError("Failure after transmission")

    monkeypatch.setattr(service.broker, "submit_package", fail)
    # Complete stock preparation first, if present.
    for _ in range(5):
        await service._setup_slice()
        for oid in list(service.broker.orders):
            await service.broker.fill(oid)
        while not service.events.empty():
            name, value = service.events.get_nowait()
            if name == "execution":
                store.execution(value)
                service.execution.on_execution(value)
            elif name == "order":
                service.execution.on_order(value)
            service.events.task_done()
        if calls:
            break
    assert len(calls) == 1
    assert len(service.execution.unresolved()) == 2
    await service._setup_slice()
    assert len(calls) == 1


async def test_package_cancel_and_rejected_preview_preserve_empty_mandatory_book(
    manifest, store, monkeypatch
):
    service, now = await setup_service(manifest, store)
    await service.control("setup")
    i, h = next(iter(service.setup_pairs.items()))

    async def reject(*args):
        return SimpleNamespace(initMarginChange="0", warningText="Guaranteed combination unavailable")

    monkeypatch.setattr(service.broker, "package_preview", reject, raising=False)
    result = await service.execution.submit_package(
        i,
        h,
        service.setup_decision_id,
        service.broker.snapshot(),
        service.last_quotes,
        service.last_scenarios,
        now,
    )
    assert not result["submitted"] and not service.broker.orders
    assert all(r["state"] == "REJECTED" for r in store.intents(manifest.account))
    assert all(service.broker.positions.get(i, 0) == 0 for i in manifest.mandatory)


def test_broker_package_is_guaranteed_limit_and_has_durable_reference(manifest):
    broker = IBKRBroker(manifest, __import__("asyncio").Queue())
    broker.native = {101: object(), 104: object()}
    req = SetupPackageRequest(
        package_id="package",
        buy_id=101,
        sell_id=104,
        quantity=1,
        limit_price=Decimal(".50"),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="decision",
    )
    contract = broker.package_contract(req)
    order = broker.package_order(req, 42)
    assert contract.secType == "BAG" and [leg.action for leg in contract.comboLegs] == ["BUY", "SELL"]
    assert order.orderType == "LMT" and order.orderRef == "hedge-package:package"
    assert [(x.tag, x.value) for x in order.smartComboRoutingParams] == [("NonGuaranteed", "0")]
    with pytest.raises(ValueError, match="Unsupported"):
        broker.package_contract(req.model_copy(update={"sell_id": 102}))


async def test_small_normal_stock_trades_are_not_authorized_by_large_plan(manifest, store, quotes, now):
    service = TradingService(manifest, store, demo=True)
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    snap = service.broker.snapshot()
    scenarios = ScenarioEngine(manifest).build(snap, service.broker.quotes(), now)
    manager = ExecutionManager(manifest, store, service.broker, RiskGate(manifest, demo=True))
    target = Target(
        quantities={**snap.quantities(), 10: 1},
        expected_score=0,
        current_score=10000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=scenarios.identifier,
        config_hash=manifest.config_hash,
    )
    assert not manager.feasible_slices(target, snap, service.broker.quotes(), scenarios, now)
    assert not store.intents(manifest.account)


def test_exposure_free_fee_burning_is_not_an_economic_trade(manifest, store, snapshot, quotes, now):
    import asyncio

    import numpy as np

    from hedge.broker import SimBroker
    from hedge.scenarios import ScenarioSet

    broker = SimBroker(manifest, asyncio.Queue(), now)
    gate = RiskGate(manifest, demo=True)
    manager = ExecutionManager(manifest, store, broker, gate)
    manager.score_context = lambda *_: (Decimal("999000"), 1000000)
    ids = tuple(sorted(quotes))
    sc = ScenarioSet(
        ids=ids,
        changes=np.zeros((4, len(ids))),
        weights=np.ones(4) / 4,
        stress=np.zeros((4, len(ids))),
        next_changes=None,
        calendar_days=0,
        source="synthetic fixture",
        identifier="zero",
    )
    # Fees would superficially reduce a positive NAV gap; no exposure benefit exists.
    assert not manager.economic_check(snapshot, quotes, sc, {**snapshot.quantities(), 10: 100}, now)


async def test_setup_plan_reduces_modeled_score_before_purchase(manifest, store):
    import numpy as np

    service, now = await setup_service(manifest, store)
    await service.control("setup")
    unhedged = np.array([manifest.mandatory.get(i, 0) for i in service.last_scenarios.ids])
    baseline = float(service.last_scenarios.weights @ np.abs(service.last_scenarios.changes @ unhedged))
    assert service.setup_target.expected_score < baseline
    assert not service.broker.positions
    assert not store.intents(manifest.account)


async def test_cancelled_package_accepts_late_matched_fills(manifest, store):
    service, now = await setup_service(manifest, store)
    await service.control("setup")
    i, h = next(iter(service.setup_pairs.items()))
    result = await service.execution.submit_package(
        i,
        h,
        service.setup_decision_id,
        service.broker.snapshot(),
        service.last_quotes,
        service.last_scenarios,
        now,
    )
    assert result["submitted"]
    await service.broker.fill(result["order_id"])
    # Deliberately deliver the cancellation callback before executions.
    ref = "hedge-package:" + result["package_id"]
    service.execution.on_order({"order_ref": ref, "order_id": result["order_id"], "status": "Cancelled"})
    while not service.events.empty():
        name, value = service.events.get_nowait()
        if name == "execution":
            store.execution(value)
            service.execution.on_execution(value)
        elif name == "order":
            service.execution.on_order(value)
        service.events.task_done()
    rows = [r for r in store.intents(manifest.account) if r.get("package_id") == result["package_id"]]
    assert all(r["state"] == "FILLED" for r in rows)
    assert not store.intents(manifest.account, True)


@pytest.mark.parametrize("recovery_blocked", [False, True])
async def test_price_amendments_are_persisted_and_capped(manifest, store, quotes, now, recovery_blocked):
    import asyncio

    from hedge.broker import SimBroker

    broker = SimBroker(manifest, asyncio.Queue(), now)
    await broker.connect()
    broker.readonly = False
    qs = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    q = qs[10].model_copy(update={"bid": 190, "ask": 190.10, "spot": 190.05})
    qs[10] = q
    broker.update_quotes(qs, now)
    snap = broker.snapshot()
    sc = ScenarioEngine(manifest).build(snap, qs, now)
    manager = ExecutionManager(manifest, store, broker, RiskGate(manifest, demo=True))
    manager.paused = False
    target = Target(
        quantities={**snap.quantities(), 10: 10},
        expected_score=0,
        current_score=1000,
        uncertainty=0,
        status="OPTIMAL",
        snapshot_id=snap.version,
        scenario_id=sc.identifier,
        config_hash=manifest.config_hash,
    )
    result = await manager.submit_slice(target, "decision", snap, qs, sc, now)
    assert result["submitted"]
    while not broker.events.empty():
        name, value = broker.events.get_nowait()
        if name == "order":
            manager.on_order(value)
        broker.events.task_done()
    later = now + timedelta(seconds=30)
    qs = {
        i: q.model_copy(update={"observed_at": later, "greek_at": later if q.greek_at else None})
        for i, q in qs.items()
    }
    broker.update_quotes(qs, later)
    manager.current_scenarios = ScenarioEngine(manifest).build(broker.snapshot(), qs, later)
    if recovery_blocked:
        manager.recovery_checks = lambda *_: ["Recovery approval expired"]
    await manager.manage(qs, later)
    row = next(r for r in store.intents(manifest.account) if r["id"] == result["intent_id"])
    if recovery_blocked:
        assert row["state"] == "CANCEL_PENDING"
        assert row["limit_price"] == Decimal("190.05")
        assert not store.records("price_amendment", manifest.account)
        assert "Recovery approval expired" in store.records("execution_wait", manifest.account)[0]["reason"]
        return
    assert row["limit_price"] == Decimal("190.10") and Decimal(row["initial_limit"]) == Decimal("190.05")
    assert store.records("price_amendment", manifest.account)
    newest = later + timedelta(seconds=1)
    qs = {
        i: q.model_copy(update={"observed_at": newest, "greek_at": newest if q.greek_at else None})
        for i, q in qs.items()
    }
    qs[10] = qs[10].model_copy(update={"bid": 190.20, "ask": 190.30, "spot": 190.25})
    broker.update_quotes(qs, newest)
    manager.current_scenarios = ScenarioEngine(manifest).build(broker.snapshot(), qs, newest)
    await manager.manage(qs, newest)
    row = next(r for r in store.intents(manifest.account) if r["id"] == result["intent_id"])
    assert row["state"] == "CANCEL_PENDING" and row["limit_price"] == Decimal("190.10")


async def test_native_market_submission_requires_policy_and_uses_market_order(manifest, quotes):
    import asyncio

    from hedge.models import utcnow

    now = utcnow()
    q = quotes[10].model_copy(
        update={
            "observed_at": now,
            "bid": 190,
            "ask": 190.01,
            "spot": 190.005,
            "bid_size": 1000,
            "ask_size": 1000,
        }
    )
    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.readonly = False
    broker.qualified = broker.reconciled = True
    broker.native = {10: SimpleNamespace(symbol="BA")}
    broker.external_quotes = lambda *_: {10: q}
    sent = []
    broker.ib.isConnected = lambda: True

    def place(contract, order):
        sent.append(order)
        return SimpleNamespace(order=order)

    broker.ib.placeOrder = place
    req = OrderRequest(
        intent_id="urgent",
        con_id=10,
        signed_quantity=100,
        limit_price=Decimal("190.01"),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="risk-correction",
        order_type="MKT",
        urgency="CORRECTIVE",
    )
    await broker.submit(req, 42)
    assert sent[0].orderType == "MKT" and sent[0].orderRef == "hedge:urgent"
    with pytest.raises(ValueError, match="urgency"):
        await broker.submit(req.model_copy(update={"urgency": "NORMAL"}), 43)
    assert len(sent) == 1


def test_broker_price_observation_preserves_initial_limit(manifest, store):
    req = OrderRequest(
        intent_id="legacy",
        con_id=10,
        signed_quantity=10,
        limit_price=Decimal("190.05"),
        account=manifest.account,
        snapshot_id="snapshot",
        decision_id="decision",
    )
    store.create_intent(req)
    store.observe_limit(req.intent_id, Decimal("190.10"))
    row = store.intents(manifest.account)[0]
    assert row["limit_price"] == Decimal("190.10") and Decimal(row["initial_limit"]) == Decimal("190.05")
    assert store.records("broker_price_observation", manifest.account)


def test_combo_aggregate_reports_are_audited_without_duplicate_asset_fill(manifest):
    import asyncio

    from hedge.models import utcnow

    queue = asyncio.Queue()
    broker = IBKRBroker(manifest, queue)
    execution = SimpleNamespace(
        acctNumber=manifest.account,
        execId="COMBO.1",
        orderRef="hedge-package:package",
        permId=42,
        shares=1,
        price=0.50,
    )
    fill = SimpleNamespace(contract=SimpleNamespace(secType="BAG"), execution=execution, time=utcnow())
    broker._execution(None, fill)
    broker._commission(None, fill, SimpleNamespace(execId="COMBO.1", commission=1.30, currency="USD"))
    events = [queue.get_nowait(), queue.get_nowait()]
    assert [name for name, value in events] == ["package_execution", "package_commission"]
    assert events[0][1]["net_price"] == "0.5" and events[1][1]["amount"] == "1.3"

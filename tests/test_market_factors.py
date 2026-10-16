import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hedge.broker import SimBroker
from hedge.execution import ExecutionManager
from hedge.models import Target
from hedge.order_policy import choose_order
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.valuation import ValuationEngine


def test_rho_and_rate_stress_use_full_option_repricing(manifest, snapshot, quotes, now):
    engine = ScenarioEngine(manifest)
    scenarios = engine.build(snapshot, quotes, now)
    assert scenarios.rhos[10] == 0
    assert scenarios.rhos[101] > 0
    assert scenarios.rhos[102] < 0
    end = max(manifest.next_close(now), now + timedelta(days=1))
    for row, rate in zip(scenarios.stress[-2:], (-0.01, 0.01), strict=True):
        expected = [
            manifest.contract_map[i].multiplier
            * engine.valuation.scenario_change(
                manifest.contract_map[i], quotes[i], now, end, 0, rate_shift=rate
            )
            for i in scenarios.ids
        ]
        np.testing.assert_allclose(row, expected)
    metrics = RiskGate(manifest).engine.evaluate(snapshot, quotes, scenarios, snapshot.quantities()).metrics
    assert metrics["rho_per_point:BA"] == pytest.approx(
        sum(
            quantity * manifest.contract_map[i].multiplier * scenarios.rhos[i] / 100
            for i, quantity in snapshot.quantities().items()
        )
    )
    assert metrics["coverage:beta"] == 0
    assert "beta_nav:portfolio" not in metrics


def test_rho_matches_small_rate_bump(manifest, now):
    engine = ValuationEngine(manifest)
    contract = manifest.contract_map[101]
    base = engine.price(contract, 195, 0.28, now)
    bumped = engine.price(contract, 195, 0.28, now, risk_free_rate=manifest.risk_free_rate + 0.00001)
    assert (bumped - base) / 0.00001 == pytest.approx(
        engine.greeks(contract, 195, 0.28, now)["rho"], rel=0.01
    )


def test_beta_is_aligned_ols_without_future_data(manifest, snapshot, quotes, now, tmp_path):
    dates = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=65)
    market = np.sin(np.arange(len(dates))) * 0.01
    rows = [
        {"date": str(day.date()), "symbol": symbol, "close": price}
        for symbol, beta in [("SPY", 1), ("BA", 2)]
        for day, price in zip(dates, 100 * np.cumprod(1 + beta * market), strict=True)
    ]
    path = tmp_path / "daily.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    config = manifest.model_copy(update={"daily_stock_history": str(path)})
    first = ScenarioEngine(config).build(snapshot, quotes, now)
    assert first.betas["BA"] == pytest.approx(2)
    metrics = RiskGate(config).engine.evaluate(snapshot, quotes, first, snapshot.quantities()).metrics
    assert metrics["beta_delta_dollars:portfolio"] == pytest.approx(2 * metrics["delta:BA"])
    assert "SPY OLS beta" in first.beta_source
    rows.extend({"date": str(now.date()), "symbol": symbol, "close": 999999} for symbol in ("BA", "SPY"))
    pd.DataFrame(rows).to_csv(path, index=False)
    assert ScenarioEngine(config).build(snapshot, quotes, now).identifier == first.identifier
    pd.DataFrame([r for r in rows if r["symbol"] == "BA"]).to_csv(path, index=False)
    betas, source = ScenarioEngine(config).market_betas(now, ["BA"])
    assert not betas and source.startswith("Unavailable")


@pytest.mark.parametrize("signed", [10, -10])
def test_urgent_limit_is_marketable_but_never_exceeds_budget(manifest, quotes, now, signed):
    config = manifest.model_copy(
        update={"execution": manifest.execution.model_copy(update={"urgent_market_stocks": False})}
    )
    broker = SimpleNamespace(price_tick=lambda *_: 0.01)
    quote = quotes[10].model_copy(update={"bid": 190.001, "ask": 190.099, "spot": 190.05})
    plan = choose_order(config, broker, manifest.contract_map[10], quote, signed, now, corrective=True)
    assert plan["order_type"] == "LMT"
    sign = 1 if signed > 0 else -1
    executable = quote.ask if signed > 0 else quote.bid
    assert (float(plan["price"]) - executable) * sign >= 0
    assert (float(plan["price"]) - plan["price_cap"]) * sign <= 1e-9
    quote = quote.model_copy(update={"bid": 190.004, "ask": 190.006})
    with pytest.raises(ValueError, match="price budget"):
        choose_order(config, broker, manifest.contract_map[10], quote, signed, now, corrective=True)


@pytest.mark.parametrize("mode", ["patient", "deadline", "paused"])
async def test_existing_order_respects_urgency_before_wait(manifest, store, quotes, now, mode):
    if mode == "deadline":
        now = now.replace(hour=15, minute=56, second=29)
    broker = SimBroker(manifest, asyncio.Queue(), now)
    await broker.connect()
    broker.readonly = False
    qs = {
        i: q.model_copy(update={"data_type": "synthetic", "observed_at": now, "greek_at": now})
        for i, q in quotes.items()
    }
    qs[10] = qs[10].model_copy(update={"bid": 190, "ask": 190.10, "spot": 190.05})
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
        event, payload = broker.events.get_nowait()
        if event == "order":
            manager.on_order(payload)
    later = now + timedelta(seconds=1)
    qs = {i: q.model_copy(update={"observed_at": later, "greek_at": later}) for i, q in qs.items()}
    broker.update_quotes(qs, later)
    manager.current_scenarios = ScenarioEngine(manifest).build(broker.snapshot(), qs, later)
    manager.pause_requested = mode == "paused"
    await manager.manage(qs, later)
    row = next(r for r in store.intents(manifest.account) if r["id"] == result["intent_id"])
    assert row["limit_price"] == Decimal("190.10" if mode == "deadline" else "190.05")
    if mode == "paused":
        assert row["state"] == "CANCEL_PENDING"
        assert not broker.orders
    else:
        assert row["state"] == "ACKNOWLEDGED"

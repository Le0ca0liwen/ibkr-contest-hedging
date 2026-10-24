import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from hedge.config import NY
from hedge.models import Contract
from hedge.optimizer import HedgeOptimizer
from hedge.recovery import (
    RecoveryConfig,
    RecoveryController,
    features,
    fit_models,
    infer,
    normalize_bars,
    promotion_errors,
    recovery_budget_errors,
    shift_scenarios,
)
from hedge.scenarios import ScenarioSet


@pytest.fixture
def bars():
    rng = np.random.default_rng(91)
    rows = []
    for symbol in ("SPY", "QQQ"):
        price = 100.0
        for day in pd.bdate_range("2026-08-03", "2026-10-07"):
            if str(day.date()) == "2026-09-07":
                continue
            clock = pd.date_range(
                datetime.combine(day.date(), datetime.min.time(), NY).replace(hour=9, minute=31),
                periods=390,
                freq="min",
            )
            for at in clock:
                opening = price
                price *= np.exp(rng.normal(0.000004, 0.0004))
                rows.append(
                    {
                        "timestamp": at,
                        "symbol": symbol,
                        "open": opening,
                        "close": price,
                        "high": max(price, opening) + 0.001,
                        "low": min(price, opening) - 0.001,
                        "volume": 1000.0,
                    }
                )
    return pd.DataFrame(rows)


@pytest.fixture
def model(bars, manifest):
    return fit_models(
        bars, manifest, RecoveryConfig(window_sessions=20), datetime(2026, 10, 7, 9, 30, tzinfo=NY)
    )


def test_frozen_models_no_future_labels_deterministic(bars, model, manifest):
    before = datetime(2026, 10, 7, 9, 30, tzinfo=NY)
    assert all(m["trained_through"] < "2026-10-07" for m in model["models"])
    changed = bars.copy()
    future = pd.to_datetime(changed.timestamp, utc=True) >= pd.Timestamp(before)
    changed.loc[future, ["open", "high", "low", "close"]] *= 2
    assert fit_models(changed, manifest, RecoveryConfig(window_sessions=20), before) == model
    forecasts = infer(model, bars, RecoveryConfig(), datetime(2026, 10, 7, 12, tzinfo=NY), manifest)
    assert len(forecasts) == 4
    assert all(
        f["horizon_end"].startswith("2026-10-07T20:00") or f["horizon_end"].startswith("2026-10-07T16:00")
        for f in forecasts
    )
    assert all(abs(f["conservative_return"]) <= abs(f["mean_return"]) for f in forecasts)


def test_model_integrity_future_and_bar_quality_blocked(bars, model, manifest):
    broken = json.loads(json.dumps(model))
    broken["models"][0]["coefficients"][0] = 99
    with pytest.raises(ValueError, match="integrity"):
        infer(broken, bars, RecoveryConfig(), datetime(2026, 10, 7, 12, tzinfo=NY), manifest)
    with pytest.raises(ValueError, match="future training"):
        infer(model, bars, RecoveryConfig(), datetime(2026, 10, 6, 12, tzinfo=NY), manifest)
    with pytest.raises(ValueError, match="Duplicate"):
        normalize_bars(pd.concat([bars, bars.iloc[:1]]))
    subset = normalize_bars(bars)
    part = subset[(subset.symbol == "SPY") & (subset.day.astype(str) == "2026-10-06")].head(90)
    assert features(part, 100, "momentum") is not None
    assert features(part.drop(part.index[-5]), 100, "momentum") is None
    yesterday = bars[pd.to_datetime(bars.timestamp, utc=True) < pd.Timestamp("2026-10-07", tz="UTC")]
    assert infer(model, yesterday, RecoveryConfig(), datetime(2026, 10, 7, 12, tzinfo=NY), manifest) == []


def test_bars_and_synthetic_results_never_promote(model, manifest):
    evidence = {
        "model_hash": model["model_hash"],
        "contest_id": manifest.contest_id,
        "config_hash": manifest.config_hash,
        "evidence_class": "bar_based_forecast_validation",
        "independent_test_windows": 20,
        "recovery_mean_score": 1,
        "hedge_mean_score": 2,
        "recovery_p90": 1,
        "hedge_p90": 2,
        "shadow_passed": True,
        "operational_fault_tests_passed": True,
        "synthetic": False,
        "forecast_trained_before_test": True,
        "bar_timestamp_verified": True,
        "volume_coverage_verified": True,
        "cash_benchmark_verified": True,
        "execution_path_validated": True,
        "provenance": "fixture",
    }
    assert any("quote-based" in r for r in promotion_errors(RecoveryConfig(), model, evidence, manifest))
    evidence["evidence_class"] = "quote_based_combined_contest_replay"
    assert not promotion_errors(RecoveryConfig(), model, evidence, manifest)
    evidence["synthetic"] = True
    assert promotion_errors(RecoveryConfig(), model, evidence, manifest)
    evidence["recovery_p90"] = float("nan")
    assert any("Finite" in r for r in promotion_errors(RecoveryConfig(), model, evidence, manifest))


def test_missing_score_evidence_is_not_reported_as_measured_underperformance(manifest):
    errors = promotion_errors(RecoveryConfig(), {}, {}, manifest)
    assert "Combined contest score results have not been recorded" in errors
    assert "Held-out mean contest score did not improve" not in errors
    assert "Held-out tail contest score deteriorated" not in errors
    measured = promotion_errors(
        RecoveryConfig(),
        {},
        {"recovery_mean_score": 3, "hedge_mean_score": 2, "recovery_p90": 4, "hedge_p90": 3},
        manifest,
    )
    assert "Held-out mean contest score did not improve" in measured
    assert "Held-out tail contest score deteriorated" in measured


@pytest.mark.parametrize("invalid", [None, "1", True, -1, float("nan"), float("inf")])
def test_invalid_score_evidence_remains_blocked(manifest, invalid):
    errors = promotion_errors(RecoveryConfig(), {}, {"recovery_mean_score": invalid}, manifest)
    assert any("Finite nonnegative" in error for error in errors)


def test_dashboard_promotion_status_tracks_evidence_changes_without_bar_loading(
    model, manifest, store, tmp_path, monkeypatch
):
    model_path = tmp_path / "model.json"
    evidence_path = tmp_path / "evidence.json"
    model_path.write_text(json.dumps(model))
    evidence_path.write_text(json.dumps({"evidence_class": "bar_based_forecast_validation"}))
    controller = RecoveryController(manifest, store)
    controller.config = controller.config.model_copy(
        update={"model_path": str(model_path), "evidence_path": str(evidence_path)}
    )
    monkeypatch.setattr(controller, "data", lambda: pytest.fail("Dashboard must not load bars"))
    first = controller.promotion_status()
    assert not first["eligible"] and first["independent_test_windows"] == 0
    assert controller.promotion_status() is first  # no repeat model/archive read
    evidence_path.write_text(json.dumps({"independent_test_windows": 5}))
    assert controller.promotion_status()["independent_test_windows"] == 5
    assert controller.mode == "off"
    evidence_path.unlink()
    assert not controller.promotion_status()["eligible"]
    assert "missing or invalid" in controller.promotion_status()["status"]


def test_recovery_joint_optimizer_budget_and_mandatory_protection(manifest, snapshot, quotes, now):
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(update={"contracts": (*manifest.contracts, spy), "allowlist": (200,)})
    quotes = {
        **quotes,
        200: quotes[10].model_copy(
            update={"con_id": 200, "spot": 100, "bid": 99.99, "ask": 100.01, "shortable_shares": None}
        ),
    }
    ids = (101, 102, 200)
    sc = ScenarioSet(
        ids,
        np.zeros((2, 3)),
        np.array([0.5, 0.5]),
        np.array([[0.0, 0.0, -20.0], [0.0, 0.0, 20.0]]),
        None,
        0.1,
        "fixture",
        "test",
    )
    forecasts = [{"symbol": "SPY", "strategy": "momentum", "conservative_return": 0.02}]
    shifted = shift_scenarios(m, sc, quotes, forecasts, "momentum")
    target = HedgeOptimizer(m).solve(
        snapshot,
        quotes,
        shifted,
        m.initial_nav,
        nav_estimate=float(m.initial_nav) - 4000,
        recovery_reference=m.mandatory,
        recovery_symbols=("SPY",),
        recovery_stress=0.0025,
    )
    assert target.status == "OPTIMAL"
    assert 0 < target.quantities[200] <= 125
    assert target.quantities[101] == target.quantities[102] == 25
    assert not recovery_budget_errors(
        m, RecoveryConfig(symbols=("SPY",)), snapshot, quotes, shifted, target.quantities, m.mandatory
    )
    bad = {**target.quantities, 200: 126}
    assert recovery_budget_errors(
        m, RecoveryConfig(symbols=("SPY",)), snapshot, quotes, shifted, bad, m.mandatory
    )


def test_recovery_shadow_never_selects_paper_and_failure_falls_back(store, manifest, tmp_path):
    m = manifest.model_copy(
        update={
            "recovery": {"mode": "shadow", "symbols": ["BA"], "model_path": str(tmp_path / "missing.json")}
        }
    )
    controller = RecoveryController(m, store)
    with pytest.raises(OSError):
        controller.set_mode("paper")
    assert controller.mode == "shadow"
    assert RecoveryConfig().incremental_stress == 0.0025
    with pytest.raises(ValueError):
        RecoveryConfig(incremental_stress=0.003)


async def test_independent_execution_slice_enforces_recovery_budget(manifest, store, quotes, now):
    import asyncio

    from hedge.broker import SimBroker
    from hedge.execution import ExecutionManager
    from hedge.models import Target
    from hedge.risk import RiskGate

    stock = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(update={"contracts": (*manifest.contracts, stock), "allowlist": (200,)})
    qs = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    qs[200] = qs[10].model_copy(update={"con_id": 200, "spot": 100, "bid": 99.99, "ask": 100.01})
    broker = SimBroker(m, asyncio.Queue(), now)
    await broker.connect()
    broker.update_quotes(qs, now)
    broker.positions[200] = 100
    ids = (101, 102, 200)
    sc = ScenarioSet(
        ids,
        np.zeros((2, 3)),
        np.array([0.5, 0.5]),
        np.array([[0.0, 0.0, -20.0], [0.0, 0.0, 20.0]]),
        None,
        0.1,
        "fixture",
        "test",
    )
    gate = RiskGate(m, demo=True)
    execution = ExecutionManager(m, store, broker, gate)
    execution.recovery_checks = lambda snap, quotes, scenarios, quantities, costs: recovery_budget_errors(
        m, RecoveryConfig(symbols=("SPY",)), snap, quotes, scenarios, quantities, m.mandatory, costs
    )
    target = Target(
        quantities={**m.mandatory, 200: 126},
        expected_score=0,
        current_score=100,
        status="OPTIMAL",
        snapshot_id=broker.snapshot().version,
        scenario_id="test",
        config_hash=m.config_hash,
        uncertainty=0,
    )
    assert not execution.feasible_slices(target, broker.snapshot(), qs, sc, now)
    execution.recovery_checks = None
    assert execution.feasible_slices(target, broker.snapshot(), qs, sc, now)


def test_recovery_fill_attribution_costs_and_day_stop_baseline(store, manifest, quotes, now):
    from decimal import Decimal

    from hedge.models import Execution, OrderRequest
    from hedge.recovery import attributed_trading_pnl

    assert attributed_trading_pnl(store, manifest, quotes, now)["daily_trading_pnl"] == 0
    decision = store.record("decision", manifest.account, {"recovery": True})
    request = OrderRequest(
        intent_id="rec-intent",
        account=manifest.account,
        con_id=10,
        signed_quantity=10,
        limit_price=Decimal("195"),
        decision_id=decision,
        snapshot_id="fixture",
    )
    store.create_intent(request)
    store.transition(request.intent_id, "VALIDATED")
    store.transition(request.intent_id, "SUBMITTING", order_id=7)
    store.transition(request.intent_id, "ACKNOWLEDGED")
    store.execution(
        Execution(
            account=manifest.account,
            exec_id="rec.01",
            order_id=7,
            con_id=10,
            signed_quantity=10,
            price=Decimal("195"),
            at=now,
        )
    )
    store.fee(manifest.account, "rec.01", Decimal("1"))
    pnl = attributed_trading_pnl(store, manifest, quotes, now)
    assert pnl["daily_trading_pnl"] == pytest.approx(-1)
    assert not pnl["commissions_provisional"]
    # Re-evaluation/restart does not reset the recorded daily baseline.
    assert attributed_trading_pnl(store, manifest, quotes, now)["daily_trading_pnl"] == pytest.approx(-1)


def test_web_recovery_controls_require_token_and_reject_unvalidated_paper(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hedge.demo import demo_manifest
    from hedge.service import TradingService
    from hedge.storage import Store
    from hedge.webapp import create_app

    monkeypatch.chdir(tmp_path)
    service = TradingService(demo_manifest(), Store("sqlite://"), demo=True)
    app = create_app(service)
    with TestClient(app) as client:
        assert client.post("/api/recovery", json={"mode": "paper"}).status_code == 403
        page = client.get("/").text
        import re

        token = re.search('name="control-token" content="([^"]+)"', page).group(1)
        response = client.post("/api/recovery", json={"mode": "paper"}, headers={"x-control-token": token})
        assert response.status_code == 409
        assert service.recovery.mode == "off"
        assert not service.broker.orders


async def test_shadow_forecast_proposal_cannot_replace_hedge_execution(
    store, manifest, quotes, now, monkeypatch
):
    from hedge.models import Target
    from hedge.service import TradingService

    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, spy), "allowlist": (200,), "recovery": {"mode": "shadow"}}
    )
    service = TradingService(m, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    qs = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    qs[200] = qs[10].model_copy(update={"con_id": 200, "spot": 100, "bid": 99.99, "ask": 100.01})
    service.broker.update_quotes(qs, now)
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = qs
    # Preserve one-symbol covariance for this execution-boundary test.
    sc = ScenarioSet(
        (101, 102, 200),
        np.zeros((2, 3)),
        np.array([0.5, 0.5]),
        np.array([[0.0, 0.0, -20.0], [0.0, 0.0, 20.0]]),
        None,
        0.1,
        "fixture",
        "test",
    )
    service.last_scenarios = sc
    service.ledger.import_close(m.day_zero, m.initial_nav, "fixture", m.account)
    recovered = Target(
        quantities={**m.mandatory, 200: 10},
        expected_score=0,
        current_score=1000,
        status="OPTIMAL",
        uncertainty=0,
        snapshot_id=service.last_snapshot.version,
        scenario_id="test",
        config_hash=m.config_hash,
    )
    monkeypatch.setattr(service.recovery, "evaluate", lambda *args: (recovered, sc, {"mode": "shadow"}))
    service.mode = "PAPER"
    await service.optimize()
    assert service.last_target.quantities.get(200, 0) != 10
    assert not store.records("decision", m.account)[0]["recovery"]
    assert not service.broker.orders


def test_forecast_rebases_to_current_price_instead_of_chasing_finished_move(manifest, quotes):
    from hedge.recovery import rebase_forecasts

    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, Contract(con_id=200, symbol="SPY", kind="STK"))}
    )
    qs = {
        **quotes,
        200: quotes[10].model_copy(update={"con_id": 200, "spot": 101, "bid": 100.99, "ask": 101.01}),
    }
    forecast = {
        "symbol": "SPY",
        "bar_price": 100,
        "mean_return": 0.005,
        "parameter_se": 0.001,
        "residual_sd": 0.005,
    }
    updated = rebase_forecasts([forecast], qs, m)[0]
    assert updated["mean_return"] == pytest.approx(100.5 / 101 - 1)
    assert updated["conservative_return"] < 0  # price has already exceeded the forecast
    assert forecast["mean_return"] == 0.005  # immutable source interpretation


def test_bar_quote_discontinuity_cannot_create_a_recovery_forecast(manifest, quotes):
    from hedge.recovery import rebase_forecasts

    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, Contract(con_id=200, symbol="SPY", kind="STK"))}
    )
    qs = {
        **quotes,
        200: quotes[10].model_copy(update={"con_id": 200, "spot": 50, "bid": 49.99, "ask": 50.01}),
    }
    with pytest.raises(ValueError, match="discontinuity"):
        rebase_forecasts(
            [
                {
                    "symbol": "SPY",
                    "bar_price": 100,
                    "mean_return": 0.002,
                    "parameter_se": 0.001,
                    "residual_sd": 0.002,
                }
            ],
            qs,
            m,
        )

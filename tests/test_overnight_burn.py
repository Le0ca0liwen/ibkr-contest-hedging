"""Overnight (close -> next reopen) objective term and the explicit surplus-burn switch."""

from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pytest

from hedge.config import NY, Manifest
from hedge.demo import demo_quotes
from hedge.optimizer import HedgeOptimizer
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine, overnight_variance
from hedge.service import TradingService


def policy(manifest, **update):
    return manifest.model_copy(update={"policy": manifest.policy.model_copy(update=update)})


def frame(manifest, snapshot, at):
    quotes = {i: q.model_copy(update={"data_type": "live"}) for i, q in demo_quotes(manifest, at).items()}
    snap = snapshot.model_copy(update={"at": at, "account_at": at})
    return snap, quotes, ScenarioEngine(manifest).build(snap, quotes, at)


def net_theta(manifest, quotes, quantities):
    return sum(quotes[i].theta * manifest.contract_map[i].multiplier * q for i, q in quantities.items())


def test_trading_time_clock_and_benchmark_helpers(manifest):
    assert overnight_variance(date(2026, 10, 22), date(2026, 10, 23)) == pytest.approx(0.30)
    assert overnight_variance(date(2026, 10, 23), date(2026, 10, 26)) == pytest.approx(0.50)
    calendar = Manifest.model_validate(
        {
            **manifest.model_dump(),
            "scoring_sessions": (*manifest.scoring_sessions, date(2026, 10, 12)),
            "scoring_rule": "calendar_interest_benchmark",
            "scoring_annual_rate": "0.0344",
        }
    )
    assert calendar.next_scored_session(date(2026, 10, 9)) == date(2026, 10, 12)
    assert calendar.next_scored_session(date(2026, 10, 12)) is None
    factor = 1 + 0.0344 / 360
    assert calendar.benchmark_growth(date(2026, 10, 12)) == pytest.approx(factor**3 - 1)
    assert calendar.benchmark_growth(date(2026, 10, 8)) == pytest.approx(factor - 1)
    assert manifest.benchmark_growth(date(2026, 10, 8)) == 0  # raw NAV scoring has no growth


def test_overnight_scenarios_reuse_shocks_on_trading_time_and_calendar_decay(manifest, snapshot):
    weekend = Manifest.model_validate(
        {**manifest.model_dump(), "scoring_sessions": (*manifest.scoring_sessions, date(2026, 10, 12))}
    )
    thursday = frame(weekend, snapshot, datetime(2026, 10, 8, 15, 30, tzinfo=NY))[2]
    friday = frame(weekend, snapshot, datetime(2026, 10, 9, 15, 30, tzinfo=NY))[2]
    final = frame(weekend, snapshot, datetime(2026, 10, 12, 15, 30, tzinfo=NY))[2]
    assert (thursday.next_session, thursday.overnight_variance) == (date(2026, 10, 9), pytest.approx(0.3))
    assert (friday.next_session, friday.overnight_variance) == (date(2026, 10, 12), pytest.approx(0.5))
    assert final.overnight_changes is None and final.next_session is None and final.next_changes is None
    stock = thursday.ids.index(10)
    for sc in (thursday, friday):
        # Same joint shocks, spot variance rescaled from the closing horizon to the gap clock.
        scale = np.sqrt(sc.overnight_variance / sc.trading_days)  # both on the trading-time clock
        assert sc.overnight_changes[:, stock] == pytest.approx(sc.changes[:, stock] * scale)
        assert sc.overnight_changes[:, stock].mean() == pytest.approx(0, abs=1e-9)  # antithetic
    straddle = np.array([25 if i in manifest.mandatory else 0 for i in thursday.ids])
    # Calendar-time decay to Monday 9:45 outweighs the larger weekend variance for a long straddle.
    assert (friday.overnight_changes @ straddle).mean() < (thursday.overnight_changes @ straddle).mean() < 0


def test_overnight_weight_hedges_straddle_decay_more_at_the_close(manifest, snapshot):
    at = datetime(2026, 10, 7, 15, 30, tzinfo=NY)
    snap, quotes, sc = frame(manifest, snapshot, at)
    on, off = policy(manifest, overnight_weight=0.5), policy(manifest, overnight_weight=0)
    hedged = HedgeOptimizer(on).solve(snap, quotes, sc, snap.nav, float(snap.nav))
    plain = HedgeOptimizer(off).solve(snap, quotes, sc, snap.nav, float(snap.nav))
    assert hedged.status == plain.status == "OPTIMAL"
    # The next-close tie-breaker is reserved for w=0, so overnight exposure is not counted twice.
    assert "next_close" in [s["stage"] for s in plain.solver_metrics["stages"]]
    assert "next_close" not in [s["stage"] for s in hedged.solver_metrics["stages"]]
    assert plain.components["overnight"] == 0 and hedged.components["overnight"] > 0
    measured = HedgeOptimizer(on)
    exposure = {
        name: measured.evaluate_target(snap, quotes, sc, target, snap.nav, float(snap.nav)).components[
            "overnight"
        ]
        for name, target in (("hedged", hedged), ("plain", plain))
    }
    hold = measured.evaluate_target(
        snap,
        quotes,
        sc,
        hedged.model_copy(update={"quantities": snap.quantities()}),
        snap.nav,
        float(snap.nav),
    ).components["overnight"]
    # w=0's within-$1 pick depends on whether its wall-clock-limited next-close ranking finishes.
    assert exposure["hedged"] < min(0.6 * hold, exposure["plain"])
    assert abs(net_theta(manifest, quotes, hedged.quantities)) < abs(
        net_theta(manifest, quotes, plain.quantities)
    )
    sold = {
        name: sum(max(0, -q) for i, q in t.quantities.items() if manifest.contract_map[i].kind == "OPT")
        for name, t in (("hedged", hedged), ("plain", plain))
    }
    assert sold["hedged"] > sold["plain"]
    assert hedged.quantities[101] == hedged.quantities[102] == 25
    gate = RiskGate(manifest).validate(snap, quotes, sc, hedged.quantities, at, 2 * hedged.estimated_cost)
    assert gate.passed, gate.reasons
    # The returned score is the certified objective, and re-evaluation reproduces it exactly.
    assert hedged.expected_score == pytest.approx(hedged.solver_metrics["returned_primary_objective"])
    again = measured.evaluate_target(snap, quotes, sc, hedged, snap.nav, float(snap.nav))
    assert again.expected_score == pytest.approx(hedged.expected_score)
    assert again.current_score == pytest.approx(hedged.current_score)


def test_options_are_frozen_after_the_option_cutoff_like_the_gate(manifest, snapshot):
    at = datetime(2026, 10, 7, 15, 50, tzinfo=NY)
    snap, quotes, sc = frame(manifest, snapshot, at)
    target = HedgeOptimizer(manifest).solve(snap, quotes, sc, snap.nav, float(snap.nav) + 300)
    assert target.status == "OPTIMAL"
    assert all(
        target.quantities.get(i, 0) == snap.quantities().get(i, 0)
        for i, c in manifest.contract_map.items()
        if c.kind == "OPT"
    )
    reasons = RiskGate(manifest).validate(snap, quotes, sc, target.quantities, at).reasons
    assert not any("Outside permitted order window" in r for r in reasons)


def test_burn_switch_spends_a_surplus_only_when_enabled(manifest, snapshot, quotes, now):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    surplus = float(snapshot.nav) + 2000
    results = {}
    for burn in (True, False):
        m = policy(manifest, burn_surplus=burn)
        optimizer = HedgeOptimizer(m)
        target = optimizer.solve(snapshot, quotes, sc, snapshot.nav, surplus)
        assert target.status == "OPTIMAL"
        assert target.expected_score == pytest.approx(target.solver_metrics["returned_primary_objective"])
        assert target.solver_metrics["returned_primary_objective"] <= (
            target.solver_metrics["primary_lower_bound"] + 1 + 1e-5
        )
        again = optimizer.evaluate_target(snapshot, quotes, sc, target, snapshot.nav, surplus)
        assert again.expected_score == pytest.approx(target.expected_score)
        assert again.components == pytest.approx(target.components)
        results[burn] = target
    burned, additive = results[True], results[False]
    assert burned.estimated_cost > 200
    # Additive still pays for genuine risk reduction (intraday variance is on trading time),
    # but spends clearly less than when the surplus itself is worth spending.
    assert additive.estimated_cost < 0.6 * burned.estimated_cost
    # A surplus larger than the cost: spending is pure burn, E|g| + c - E|g - c| = 2c.
    assert burned.components["burn"] == pytest.approx(2 * burned.components["scored_cost"], rel=0.05)
    assert additive.components["burn"] <= 2 * additive.components["scored_cost"] + 1e-6
    assert additive.components["closing"] >= additive.components["scored_cost"]  # additive: E|g| + c


@pytest.mark.parametrize("burn", [True, False])
async def test_decision_records_cost_treatment_and_burn_component(store, manifest, quotes, now, burn):
    m = policy(manifest, burn_surplus=burn)
    service = TradingService(m, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    service.broker.update_quotes(
        {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}, now
    )
    service.last_snapshot = service.broker.snapshot()
    service.last_quotes = service.broker.quotes()
    service.last_scenarios = ScenarioEngine(m).build(service.last_snapshot, service.last_quotes, now)
    service.ledger.import_close(m.day_zero, m.initial_nav - Decimal(2000), "fixture", m.account)
    service.force_optimize = True
    service.mode = "SHADOW"
    await service.optimize()
    record = store.records("decision", m.account)[0]
    assert record["cost_treatment"] == ("burn_surplus" if burn else "additive")
    assert record["burn_component"] == pytest.approx(service.last_target.components["burn"])
    assert record["overnight_score"] == pytest.approx(service.last_target.components["overnight"])
    assert record["components"]["closing"] + record["components"]["overnight"] == pytest.approx(
        record["expected_score"]
    )
    assert not service.broker.orders

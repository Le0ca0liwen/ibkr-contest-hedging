from datetime import timedelta

import numpy as np
import pytest

from hedge.config import Manifest
from hedge.models import Dividend, Position
from hedge.optimizer import HedgeOptimizer, execution_cost
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine, ScenarioSet
from hedge.valuation import ValuationEngine


def test_american_put_not_less_than_european(manifest, now):
    engine = ValuationEngine(manifest)
    put = manifest.contract_map[102]
    assert engine.price(put, 175, 0.28, now) >= engine.price(put, 175, 0.28, now, european=True) - 0.02


def test_straddle_gamma_vega_and_theta(manifest, now):
    engine = ValuationEngine(manifest)
    contracts = [manifest.contract_map[101], manifest.contract_map[102]]
    greeks = [engine.greeks(c, 195, 0.28, now) for c in contracts]
    assert sum(g["gamma"] for g in greeks) > 0
    assert sum(g["vega"] for g in greeks) > 0
    assert sum(g["theta"] for g in greeks) < 0
    assert abs(sum(g["delta"] for g in greeks)) < 0.15
    morning = sum(engine.price(c, 195, 0.28, now) for c in contracts)
    evening = sum(engine.price(c, 195, 0.28, now + timedelta(hours=6)) for c in contracts)
    assert evening < morning


def test_option_multiplies_per_share_price_by_100(manifest, now, quotes, snapshot):
    scenarios = ScenarioEngine(manifest).build(snapshot, quotes, now)
    c = manifest.contract_map[101]
    assert c.multiplier == 100
    assert scenarios.changes[:, scenarios.ids.index(101)].std() > 1


def test_dividend_schedule_and_weekend_time(manifest, now):
    data = manifest.model_dump()
    data["dividends"] = {"BA": [Dividend(ex_date=now.date() + timedelta(days=1), amount=2)]}
    m = Manifest.model_validate(data)
    call = manifest.contract_map[101]
    without = ValuationEngine(manifest).price(call, 200, 0.3, now)
    with_dividend = ValuationEngine(m).price(call, 200, 0.3, now)
    assert with_dividend < without + 0.05
    put = manifest.contract_map[102]
    assert ValuationEngine(manifest).price(put, 195, 0.3, now + timedelta(days=3)) < ValuationEngine(
        manifest
    ).price(put, 195, 0.3, now)


def test_optimizer_preserves_mandates_integer_and_real_costs(manifest, snapshot, quotes, now):
    scenarios = ScenarioEngine(manifest).build(snapshot, quotes, now)
    result = HedgeOptimizer(manifest).solve(snapshot, quotes, scenarios, manifest.initial_nav)
    assert result.status == "OPTIMAL"
    assert all(result.quantities[i] == q for i, q in manifest.mandatory.items())
    assert all(isinstance(q, int) for q in result.quantities.values())
    changes = {i: result.quantities.get(i, 0) - snapshot.quantities().get(i, 0) for i in manifest.allowlist}
    assert result.estimated_cost == pytest.approx(
        execution_cost(manifest, quotes, list(changes), list(changes.values()))
    )
    assert result.expected_score < result.current_score
    assert (
        sum(i in result.quantities for i in manifest.allowlist if manifest.contract_map[i].kind == "OPT") <= 6
    )


def test_no_fictitious_cost_when_positions_cannot_trade(manifest, snapshot, quotes):
    m = manifest.model_copy(update={"allowlist": ()})
    ids = (101, 102)
    sc = ScenarioSet(
        ids,
        np.array([[10.0, -2.0], [-10.0, 2.0]]),
        np.array([0.5, 0.5]),
        np.zeros((1, 2)),
        None,
        0.25,
        "test",
        "test",
    )
    result = HedgeOptimizer(m).solve(snapshot, quotes, sc, manifest.initial_nav)
    assert result.estimated_cost == 0
    assert result.expected_score == 200
    assert result.quantities == snapshot.quantities()


def test_identical_snapshot_yields_identical_decision(manifest, snapshot, quotes, now):
    engine = ScenarioEngine(manifest)
    one, two = engine.build(snapshot, quotes, now), engine.build(snapshot, quotes, now)
    assert np.array_equal(one.changes, two.changes)
    opt = HedgeOptimizer(manifest)
    assert (
        opt.solve(snapshot, quotes, one, manifest.initial_nav).quantities
        == opt.solve(snapshot, quotes, two, manifest.initial_nav).quantities
    )


def test_infeasible_solution_does_not_relax_limits(manifest, snapshot, quotes, now):
    m = manifest.model_copy(
        update={"allowlist": (10,), "risk": manifest.risk.model_copy(update={"stress_loss": 0.000001})}
    )
    sc = ScenarioEngine(m).build(snapshot, quotes, now)
    result = HedgeOptimizer(m).solve(snapshot, quotes, sc, m.initial_nav)
    assert result.status != "OPTIMAL"
    assert result.quantities == snapshot.quantities()


@pytest.mark.parametrize("strategy", ["delta", "greek", "min_variance", "historical_l1", "two_close"])
def test_baseline_solvers_return_only_valid_integer_targets(manifest, snapshot, quotes, now, strategy):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    result = HedgeOptimizer(manifest).solve(snapshot, quotes, sc, manifest.initial_nav, strategy=strategy)
    assert all(result.quantities[i] == q for i, q in manifest.mandatory.items())
    assert all(isinstance(q, int) for q in result.quantities.values())


@pytest.mark.parametrize(
    "alter", ["stale", "delayed", "missing_greeks", "wrong_account", "mandatory", "unknown", "disconnected"]
)
def test_gate_blocks_bad_broker_state(manifest, snapshot, quotes, now, alter):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    proposed = snapshot.quantities()
    if alter == "stale":
        snapshot = snapshot.model_copy(update={"account_at": now - timedelta(minutes=5)})
    elif alter == "delayed":
        quotes[101] = quotes[101].model_copy(update={"data_type": "delayed"})
    elif alter == "missing_greeks":
        quotes[101] = quotes[101].model_copy(update={"greek_at": None})
    elif alter == "wrong_account":
        snapshot = snapshot.model_copy(update={"account": "U_LIVE"})
    elif alter == "mandatory":
        proposed[101] = 24
    elif alter == "unknown":
        proposed[999] = 100
    elif alter == "disconnected":
        snapshot = snapshot.model_copy(update={"connected": False})
    assert not RiskGate(manifest).validate(snapshot, quotes, sc, proposed, now).passed


@pytest.mark.parametrize("field", ["account_at", "greek_at"])
@pytest.mark.parametrize("seconds", [1, 2])
def test_future_risk_inputs_respect_clock_tolerance(manifest, snapshot, quotes, now, field, seconds):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    gate = RiskGate(manifest)
    assert gate.validate(snapshot, quotes, sc, snapshot.quantities(), now).passed
    timestamp = now + timedelta(seconds=seconds)
    if field == "account_at":
        snapshot = snapshot.model_copy(update={field: timestamp})
    else:
        quotes = {**quotes, 101: quotes[101].model_copy(update={field: timestamp})}
    result = gate.validate(snapshot, quotes, sc, snapshot.quantities(), now)
    assert result.passed == (seconds <= 1)


def test_excessive_short_options_rejected(manifest, snapshot, quotes, now):
    sc = ScenarioEngine(manifest).build(snapshot, quotes, now)
    proposed = {**snapshot.quantities(), 104: -51}
    assert not RiskGate(manifest).validate(snapshot, quotes, sc, proposed, now).passed


def test_existing_out_of_screen_position_is_included(manifest, snapshot, quotes, now):
    from hedge.universe import screen_options

    modified = snapshot.model_copy(
        update={"positions": (*snapshot.positions, Position(con_id=106, quantity=-1))}
    )
    ids = screen_options(manifest, quotes, modified.quantities())
    assert 106 in ids


def test_day_zero_forecasts_from_baseline_close_not_current_nav(manifest, snapshot, quotes, now):
    setup_now = now - timedelta(days=1)
    snap = snapshot.model_copy(update={"at": setup_now, "account_at": setup_now})
    shifted = {
        i: q.model_copy(update={"observed_at": setup_now, "greek_at": setup_now}) for i, q in quotes.items()
    }
    sc = ScenarioEngine(manifest).build(snap, shifted, setup_now)
    assert sc.calendar_days == 1
    result = HedgeOptimizer(manifest).solve(snap, shifted, sc, 12345)
    other = HedgeOptimizer(manifest).solve(snap, shifted, sc, 9999999)
    assert result.quantities == other.quantities


def test_optimizer_never_selects_frozen_or_stale_new_hedges(manifest, snapshot, quotes, now):
    from datetime import timedelta

    from hedge.optimizer import HedgeOptimizer
    from hedge.scenarios import ScenarioEngine

    bad = dict(quotes)
    for i in manifest.allowlist:
        bad[i] = bad[i].model_copy(update={"data_type": "frozen"})
    bad[10] = quotes[10].model_copy(update={"observed_at": now - timedelta(seconds=6)})
    scenarios = ScenarioEngine(manifest).build(snapshot, bad, now)
    target = HedgeOptimizer(manifest).solve(snapshot, bad, scenarios, snapshot.nav)
    assert target.status == "NO_CANDIDATES"
    assert target.quantities == snapshot.quantities()
    assert target.estimated_cost == 0


def test_nav_deficit_is_in_objective_and_can_select_target_closing_trade(manifest, snapshot, quotes):
    # Controlled positive-return assumptions prove mechanics, not a real forecast.
    m = manifest.model_copy(update={"allowlist": (10,)})
    ids = (10, 101, 102)
    scenarios = ScenarioSet(
        ids,
        np.array([[10.0, 0.0, 0.0], [20.0, 0.0, 0.0]]),
        np.array([0.5, 0.5]),
        np.array([[-39.0, 0.0, 0.0], [39.0, 0.0, 0.0]]),
        None,
        0.1,
        "controlled fixture",
        "deficit-fixture",
    )
    target = HedgeOptimizer(m).solve(
        snapshot, quotes, scenarios, m.initial_nav, nav_estimate=float(m.initial_nav) - 4000
    )
    assert target.status == "OPTIMAL"
    assert target.current_score == pytest.approx(4000)
    assert target.expected_score < target.current_score - 1000
    assert target.quantities[10] > 0
    assert target.quantities[101] == target.quantities[102] == 25
    at_target = HedgeOptimizer(m).solve(
        snapshot, quotes, scenarios, m.initial_nav, nav_estimate=float(m.initial_nav)
    )
    assert at_target.quantities.get(10, 0) == 0


def _sp500_book(manifest, snapshot, held):
    """The BA fixture renamed: options on SPY, shares of IVV (one index, one exposure group)."""
    data = manifest.model_dump()
    data["contracts"] = [{**c, "symbol": "IVV" if c["kind"] == "STK" else "SPY"} for c in data["contracts"]]
    m = Manifest.model_validate(data)
    positions = tuple(Position(con_id=i, quantity=q) for i, q in held.items())
    return m, snapshot.model_copy(update={"positions": positions})


def test_sp500_funds_share_one_residual_delta_cap(manifest, snapshot, quotes, now):
    """Oct 28: SPY options carried $94-98k of delta against the $98k cap while 20 IVV added $15.5k.
    The funds track one index: an IVV short offsets SPY options' delta, an IVV long adds to it."""
    from hedge.risk import RiskEngine

    m, snap = _sp500_book(manifest, snapshot, {**manifest.mandatory, 104: 20})
    sc = ScenarioEngine(manifest).build(snap, quotes, now)  # same contracts, one underlying's history
    engine = RiskEngine(m)
    alone = engine.evaluate(snap, quotes, sc, snap.quantities())
    options = alone.metrics["delta:SPY"]
    cap = m.risk.residual_delta * alone.metrics["budget"]
    assert options > cap and "Residual delta cap: S&P 500" in alone.reasons
    shares = -round(options / quotes[10].spot)  # an IVV short the size of the options' delta
    hedged = engine.evaluate(snap, quotes, sc, {**snap.quantities(), 10: shares})
    assert hedged.metrics["residual_delta:S&P 500"] == pytest.approx(
        hedged.metrics["delta:SPY"] + hedged.metrics["delta:IVV"]
    )
    assert abs(hedged.metrics["residual_delta:S&P 500"]) < quotes[10].spot
    assert not any(r.startswith("Residual delta cap") for r in hedged.reasons)
    adding = engine.evaluate(snap, quotes, sc, {**snap.quantities(), 10: 100})
    assert "Residual delta cap: S&P 500" in adding.reasons


def test_the_delta_solve_hedges_spy_options_with_ivv_shares(manifest, snapshot, quotes, now):
    m, snap = _sp500_book(manifest, snapshot, {**manifest.mandatory, 104: 20})
    m = m.model_copy(update={"allowlist": (10,)})  # options frozen: only IVV shares can trade
    sc = ScenarioEngine(manifest).build(snap, quotes, now)
    result = HedgeOptimizer(m).solve(snap, quotes, sc, m.initial_nav, strategy="delta")
    options = sum(q * 100 * quotes[i].delta * quotes[i].spot for i, q in snap.quantities().items() if i != 10)
    assert result.quantities[10] == pytest.approx(-options / quotes[10].spot, abs=2)
    assert RiskGate(m).validate(snap, quotes, sc, result.quantities, now).passed


def test_gross_can_count_options_at_their_delta(manifest, snapshot, quotes, now):
    """Oct 28: 37 short SPY Nov 27 options used $2.85M of the $2.95M gross cap at full notional
    while carrying little delta; risk.gross_basis "delta" counts them at |delta| x notional."""
    from hedge.risk import RiskEngine

    held = {**snapshot.quantities(), 104: -20, 10: 300}
    snap = snapshot.model_copy(
        update={"positions": tuple(Position(con_id=i, quantity=q) for i, q in held.items())}
    )
    sc = ScenarioEngine(manifest).build(snap, quotes, now)
    by_delta = manifest.model_copy(update={"risk": manifest.risk.model_copy(update={"gross_basis": "delta"})})
    notional = RiskEngine(manifest).evaluate(snap, quotes, sc, held).metrics["gross_notional"]
    delta = RiskEngine(by_delta).evaluate(snap, quotes, sc, held).metrics["gross_notional"]
    shares = 300 * quotes[10].spot
    assert notional == pytest.approx(shares + 20 * 100 * quotes[104].spot)
    assert delta == pytest.approx(shares + 20 * 100 * quotes[104].spot * abs(quotes[104].delta))
    result = HedgeOptimizer(by_delta).solve(snap, quotes, sc, by_delta.initial_nav)
    assert result.status == "OPTIMAL"
    assert RiskGate(by_delta).validate(snap, quotes, sc, result.quantities, now).passed

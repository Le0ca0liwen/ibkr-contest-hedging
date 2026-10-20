"""Correctness of tighter models, objective certificates and ranking recovery."""

from dataclasses import replace
from decimal import Decimal

import cvxpy as cp
import numpy as np
import pytest

from hedge.cashflows import cash_carry
from hedge.models import Position
from hedge.optimizer import HedgeOptimizer, _positive_part, _stress_projection, execution_cost
from hedge.scenarios import ScenarioSet


@pytest.mark.parametrize("lower,upper,value", [(-10, 10, -5), (-10, 10, 5), (-10, 0, -5), (0, 10, 5)])
def test_exact_positive_part_cannot_invent_cashflow(lower, upper, value):
    x = cp.Variable()
    constraints = [x == value]
    positive = _positive_part(x, lower, upper, constraints)
    # Maximize the expense/credit to expose any freely adjustable split.
    problem = cp.Problem(cp.Maximize(positive), constraints)
    problem.solve(solver=cp.HIGHS, highs_options={"threads": 1})
    assert problem.status == "optimal"
    assert problem.value == pytest.approx(max(value, 0))
    binaries = sum(v.size for v in problem.variables() if v.attributes["boolean"])
    assert binaries == (1 if lower < 0 < upper else 0)


def test_stress_projection_preserves_strongest_exact_rows():
    stress = np.array([[1.0, 2.0, 4.0], [1.0, 2.0, 5.0], [-1.0, 3.0, 7.0], [1.0 + 1e-12, 2.0, 4.0]])
    fixed = np.array([-3.0, -7.0, 2.0, -9.0])
    matrix, offset = _stress_projection(stress, [0, 1], fixed)
    assert len(matrix) == 3  # Near duplicates are deliberately not rounded.
    for h in ([-20, 10], [0, 0], [8, -3]):
        assert np.min(matrix @ h + offset) == pytest.approx(np.min(stress[:, :2] @ h + fixed))


def small_problem(manifest, snapshot, quotes, cash, held, gap):
    m = manifest.model_copy(
        update={
            "allowlist": (10,),
            "risk": manifest.risk.model_copy(update={"stock_notional": 0.0005}),
            "execution": manifest.execution.model_copy(update={"minimum_stock_notional": 0}),
            "policy": manifest.policy.model_copy(
                update={
                    "cash_rate": 0.0335,
                    "cash_rate_status": "modeled",
                    "cash_rate_source": "test",
                }
            ),
        }
    )
    qs = dict(quotes)
    qs[10] = qs[10].model_copy(update={"bid_size": 3, "ask_size": 2, "shortable_shares": 10})
    s = snapshot.model_copy(
        update={
            "cash": Decimal(str(cash)),
            "positions": (*snapshot.positions, Position(con_id=10, quantity=held)),
        }
    )
    sc = ScenarioSet(
        (10, 101, 102),
        np.array([[10.0, 0.0, 0.0], [20.0, 0.0, 0.0]]),
        np.array([0.5, 0.5]),
        np.zeros((1, 3)),
        None,
        0.1,
        "controlled fixture, no trading evidence",
        "small-runtime-fixture",
    )
    return m, s, qs, sc, float(s.nav) + gap


@pytest.mark.parametrize("cash", [-1000, 10000, 100000])
@pytest.mark.parametrize("held,gap", [(-2, -25), (2, 25)])
def test_reduced_model_matches_enumerated_integer_cost_and_carry(manifest, snapshot, quotes, cash, held, gap):
    m, s, qs, sc, estimate = small_problem(manifest, snapshot, quotes, cash, held, gap)
    opt = HedgeOptimizer(m)
    target = opt.solve(s, qs, sc, s.nav, estimate)
    assert target.status == "OPTIMAL"
    scores = []
    for h in range(-2, 3):
        fee = execution_cost(m, qs, [10], [h - held])
        cash_after = cash - qs[10].mid * (h - held) - fee
        carry = cash_carry(m, cash_after, max(0, -h) * qs[10].spot, sc.calendar_days, float(s.nav))
        scores.append(float(sc.weights @ np.abs(gap + sc.changes[:, 0] * h + carry - fee)))
    assert target.expected_score <= min(scores) + 1 + 1e-5
    assert target.estimated_cost == pytest.approx(
        execution_cost(m, qs, [10], [target.quantities.get(10, 0) - held])
    )
    reevaluated = opt.evaluate_target(s, qs, sc, target, s.nav, estimate)
    assert target.expected_score == pytest.approx(reevaluated.expected_score)
    assert target.quantities[101] == target.quantities[102] == 25
    assert target.solver_metrics["returned_primary_objective"] <= (
        target.solver_metrics["primary_lower_bound"] + 1 + 1e-5
    )


def test_ranking_timeout_never_uses_failed_solution(manifest, snapshot, quotes, monkeypatch):
    m, s, qs, sc, estimate = small_problem(manifest, snapshot, quotes, 100000, -2, -25)
    original, calls = cp.Problem.solve, []

    def fail_ranking(problem, *args, **kwargs):
        result = original(problem, *args, **kwargs)
        calls.append(1)
        if len(calls) > 1:
            problem._status = cp.USER_LIMIT
            for variable in problem.variables():
                variable.save_value(np.full(variable.shape, 1000000))
        return result

    monkeypatch.setattr(cp.Problem, "solve", fail_ranking)
    target = HedgeOptimizer(m).solve(s, qs, sc, s.nav, estimate)
    assert target.status == "OPTIMAL"
    assert target.solver_metrics["accepted_stage"] == "primary"
    assert not target.solver_metrics["ranking_complete"]
    assert -2 <= target.quantities.get(10, 0) <= 2
    assert target.expected_score < 15


def test_nonfinite_certificate_rejects_primary(manifest, snapshot, quotes, monkeypatch):
    m, s, qs, sc, estimate = small_problem(manifest, snapshot, quotes, 100000, -2, -25)
    original = cp.Problem.solve

    def invalid_bound(problem, *args, **kwargs):
        result = original(problem, *args, **kwargs)
        problem.solver_stats.extra_stats.mip_dual_bound = float("nan")
        return result

    monkeypatch.setattr(cp.Problem, "solve", invalid_bound)
    target = HedgeOptimizer(m).solve(s, qs, sc, s.nav, estimate)
    assert target.status == "OBJECTIVE_CERTIFICATE_FAILED"
    assert target.quantities == s.quantities()


def test_cardinality_constant_does_not_invalidate_turnover_certificate(manifest, snapshot, quotes):
    m = manifest.model_copy(
        update={
            "allowlist": (10,),
            "execution": manifest.execution.model_copy(update={"minimum_stock_notional": 0}),
            "policy": manifest.policy.model_copy(update={"borrow_rate": 0, "financing_rate": 0}),
        }
    )
    qs = dict(quotes)
    qs[10] = qs[10].model_copy(update={"shortable_shares": 3000})
    qs[101] = qs[101].model_copy(update={"delta": 1})
    qs[102] = qs[102].model_copy(update={"delta": 0})
    s = snapshot.model_copy(update={"positions": (*snapshot.positions, Position(con_id=10, quantity=-2200))})
    sc = ScenarioSet(
        (10, 101, 102),
        np.zeros((2, 3)),
        np.array([0.5, 0.5]),
        np.zeros((1, 3)),
        None,
        0.1,
        "fixture",
        "fixed-nonzero-stock",
    )
    target = HedgeOptimizer(m).solve(s, qs, sc, s.nav)
    assert target.status == "OPTIMAL"
    assert target.solver_metrics["ranking_complete"]
    assert target.quantities[10] == -2200


def test_failed_next_close_is_not_reported_as_complete_ranking(manifest, snapshot, quotes, monkeypatch):
    m, s, qs, sc, estimate = small_problem(manifest, snapshot, quotes, 100000, -2, -25)
    sc = replace(sc, next_changes=sc.changes.copy())
    original, calls = cp.Problem.solve, []

    def fail_next(problem, *args, **kwargs):
        result = original(problem, *args, **kwargs)
        calls.append(1)
        if len(calls) == 2:
            problem._status = cp.USER_LIMIT
        return result

    monkeypatch.setattr(cp.Problem, "solve", fail_next)
    target = HedgeOptimizer(m).solve(s, qs, sc, s.nav, estimate)
    assert target.status == "OPTIMAL"
    assert target.solver_metrics["accepted_stage"] == "primary"
    assert not target.solver_metrics["turnover_complete"]
    assert not target.solver_metrics["ranking_complete"]
    assert "next_selection_limit" not in target.solver_metrics


def test_deadline_includes_model_building(manifest, snapshot, quotes, monkeypatch):
    m, s, qs, sc, estimate = small_problem(manifest, snapshot, quotes, 100000, -2, -25)
    m = m.model_copy(update={"policy": m.policy.model_copy(update={"solver_seconds": 1e-12})})

    def no_solve(*args, **kwargs):
        raise AssertionError("Solver must not run after its budget expires")

    monkeypatch.setattr(cp.Problem, "solve", no_solve)
    target = HedgeOptimizer(m).solve(s, qs, sc, s.nav, estimate)
    assert target.status == "TIMEOUT"
    assert target.quantities == s.quantities()


def test_primary_band_is_configurable_and_bounded(manifest):
    import pytest

    from hedge.config import TradingPolicy

    assert TradingPolicy().primary_band == 1  # default keeps the original $1 certificate
    assert TradingPolicy(primary_band=20).primary_band == 20
    with pytest.raises(ValueError):
        TradingPolicy(primary_band=100)

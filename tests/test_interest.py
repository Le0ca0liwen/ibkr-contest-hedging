from datetime import timedelta
from decimal import Decimal

import numpy as np
import pytest

from hedge.cashflows import cash_carry
from hedge.config import Manifest, TradingPolicy
from hedge.optimizer import HedgeOptimizer
from hedge.scenarios import ScenarioSet
from hedge.scoring import ScoreLedger
from hedge.valuation import NAVTracker


def interest_manifest(manifest):
    return Manifest.model_validate(
        {
            **manifest.model_dump(),
            "policy": {
                **manifest.policy.model_dump(),
                "cash_rate": 0.0313,
                "cash_rate_status": "modeled",
                "cash_rate_source": "Explicit test rate",
            },
        }
    )


def test_existing_accrued_interest_is_not_added_again(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    s = snapshot.model_copy(update={"nav": Decimal("1000278.85"), "accrued_cash": Decimal("278.85")})
    assert tracker.estimate(s, quotes, manifest.contract_map) == 1000278.85
    refreshed = s.model_copy(
        update={
            "nav": Decimal("1000364.85"),
            "accrued_cash": Decimal("364.85"),
            "account_at": now + timedelta(seconds=1),
        }
    )
    assert tracker.estimate(refreshed, quotes, manifest.contract_map) == 1000364.85
    # Interest posting transfers accrued interest to ordinary cash, not new NAV.
    posted = refreshed.model_copy(
        update={
            "cash": refreshed.cash + Decimal("364.85"),
            "accrued_cash": Decimal(0),
            "account_at": now + timedelta(seconds=2),
        }
    )
    assert tracker.estimate(posted, quotes, manifest.contract_map) == 1000364.85


def test_interest_change_counts_in_score_only_once(manifest, store):
    ledger = ScoreLedger(store, manifest)
    ledger.import_close(
        manifest.day_zero, Decimal("1000278.85"), "Interest-inclusive official report", manifest.account
    )
    ledger.import_close(
        manifest.scoring_sessions[0],
        Decimal("1000364.85"),
        "Interest-inclusive official report",
        manifest.account,
    )
    ledger.import_close(
        manifest.scoring_sessions[1],
        Decimal("1000364.85"),
        "Accrued interest posted to cash",
        manifest.account,
    )
    assert Decimal(ledger.summary()["total"]) == Decimal("86.00")


def test_future_interest_forecast_threshold_nav_scale_and_weekend(manifest):
    m = interest_manifest(manifest)
    assert cash_carry(m, 1000000, 0, 1, 1000000) == pytest.approx(86.075)
    assert cash_carry(m, 1000000, 0, 3, 1000000) == pytest.approx(258.225)
    assert cash_carry(m, 10000, 0, 1, 1000000) == 0
    assert cash_carry(m, 1000000, 0, 1, 50000) == pytest.approx(43.0375)
    # Stock-short sale proceeds are not all freely interest-eligible cash.
    assert cash_carry(m, 1000000, 100000, 1, 1000000) < 86.075


def test_optimizer_counts_incremental_interest_without_adding_accrual(manifest, snapshot, quotes):
    m = interest_manifest(manifest).model_copy(update={"allowlist": ()})
    s = snapshot.model_copy(
        update={"nav": Decimal("1000278.85"), "cash": Decimal("1000000"), "accrued_cash": Decimal("278.85")}
    )
    ids = tuple(sorted(m.contract_map))
    sc = ScenarioSet(
        ids,
        np.zeros((2, len(ids))),
        np.array([0.5, 0.5]),
        np.zeros((1, len(ids))),
        None,
        1,
        "interest test",
        "interest-fixture",
    )
    result = HedgeOptimizer(m).solve(s, quotes, sc, Decimal("1000278.85"))
    assert result.current_score == pytest.approx(86.075)
    assert result.expected_score == pytest.approx(86.075)
    # Interest belongs in closing-NAV error, not an exemption from the score.
    wrong_cash_baseline = HedgeOptimizer(m).solve(s, quotes, sc, Decimal("1000000"))
    assert wrong_cash_baseline.current_score == pytest.approx(278.85 + 86.075)


def test_positive_interest_rate_requires_provenance():
    with pytest.raises(ValueError, match="provenance"):
        TradingPolicy(cash_rate=0.03)


def test_integer_optimizer_interest_matches_revalidated_cashflow(manifest, snapshot, quotes, now):
    from hedge.scenarios import ScenarioEngine

    m = interest_manifest(manifest)
    sc = ScenarioEngine(m).build(snapshot, quotes, now)
    optimizer = HedgeOptimizer(m)
    target = optimizer.solve(snapshot, quotes, sc, snapshot.nav)
    assert target.status == "OPTIMAL"
    checked = optimizer.evaluate_target(snapshot, quotes, sc, target, snapshot.nav, float(snapshot.nav))
    assert target.expected_score == pytest.approx(checked.expected_score, abs=1e-5)
    assert target.current_score == pytest.approx(checked.current_score, abs=1e-5)


async def test_synthetic_accrual_increases_nav_not_cash_twice(manifest, now):
    import asyncio

    from hedge.broker import SimBroker
    from hedge.demo import demo_quotes

    m = interest_manifest(manifest)
    broker = SimBroker(m, asyncio.Queue(), now, start_empty=True)
    await broker.connect()
    broker.update_quotes(demo_quotes(m, now), now)
    tomorrow = now + timedelta(days=1)
    broker.update_quotes(demo_quotes(m, tomorrow), tomorrow)
    assert broker.cash == Decimal("1000000")
    assert float(broker.accrued_cash) == pytest.approx(86.075)
    assert float(broker.nav) == pytest.approx(1000086.075)


def test_with_the_statement_interest_in_the_estimate_the_closing_carry_has_no_cash_credit(
    manifest, snapshot, quotes
):
    """policy.statement_interest: the service adds the day's statement interest to the closing
    estimate (three days on a Monday), so the optimizer must not credit cash interest again."""
    m = interest_manifest(manifest)
    assert cash_carry(m, 1000000, 0, 1, 1000000, with_credit=False) == 0
    assert cash_carry(m, 1000000, 100000, 1, 1000000, with_credit=False) == pytest.approx(
        -100000 * 0.05 / 360
    )
    m = m.model_copy(
        update={"allowlist": (), "policy": m.policy.model_copy(update={"statement_interest": True})}
    )
    s = snapshot.model_copy(update={"nav": Decimal("1000278.85"), "cash": Decimal("1000000")})
    ids = tuple(sorted(m.contract_map))
    sc = ScenarioSet(
        ids, np.zeros((2, len(ids))), np.array([0.5, 0.5]), np.zeros((1, len(ids))), None, 1, "t", "t"
    )
    result = HedgeOptimizer(m).solve(s, quotes, sc, Decimal("1000278.85"))
    assert result.current_score == pytest.approx(0) and result.expected_score == pytest.approx(0)

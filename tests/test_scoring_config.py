from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from hedge.config import NY, Manifest, load_manifest
from hedge.scoring import ScoreLedger


def test_round_trip_is_10000_not_zero(store, manifest):
    ledger = ScoreLedger(store, manifest)
    ledger.import_close(manifest.day_zero, Decimal(1000000), "day 0 report", manifest.account)
    assert ledger.summary()["total"] == "0"
    ledger.import_close(manifest.scoring_sessions[0], Decimal(1005000), "day 1", manifest.account)
    ledger.import_close(manifest.scoring_sessions[1], Decimal(1000000), "day 2", manifest.account)
    assert Decimal(ledger.summary()["total"]) == 10000
    assert ledger.previous_close(manifest.scoring_sessions[1]) == 1005000


def test_revision_recalculates_both_neighbors(store, manifest):
    ledger = ScoreLedger(store, manifest)
    for day, value in zip(
        (manifest.day_zero, *manifest.scoring_sessions), (1000000, 1005000, 1001000, 1001000), strict=True
    ):
        ledger.import_close(day, Decimal(value), "original", manifest.account)
    assert Decimal(ledger.summary()["total"]) == 9000
    ledger.import_close(manifest.scoring_sessions[0], Decimal(1002000), "correction", manifest.account)
    assert Decimal(ledger.summary()["total"]) == 3000
    assert ledger.summary()["complete"]


def test_missing_close_does_not_bridge_days(store, manifest):
    ledger = ScoreLedger(store, manifest)
    ledger.import_close(manifest.day_zero, Decimal(1000000), "baseline", manifest.account)
    ledger.import_close(manifest.scoring_sessions[1], Decimal(999000), "later", manifest.account)
    assert ledger.summary()["days"][1]["score"] is None
    assert Decimal(ledger.summary()["total"]) == 0


def test_wrong_account_nonfinite_and_atomic_import(store, manifest, tmp_path):
    ledger = ScoreLedger(store, manifest)
    with pytest.raises(ValueError):
        ledger.import_close(manifest.day_zero, Decimal(1), "source", "U_LIVE")
    with pytest.raises(ValueError):
        ledger.import_close(manifest.day_zero, Decimal("NaN"), "source", manifest.account)
    path = tmp_path / "reports.csv"
    path.write_text("date,nav,source,account\n2026-10-06,1000000,ok,DU_TEST\n2026-10-07,1,bad,U_LIVE\n")
    with pytest.raises(ValueError):
        ledger.import_csv(path)
    assert ledger.closes() == {}


@pytest.mark.parametrize(
    "change",
    [
        {"account": "U123"},
        {"environment": "live"},
        {"day_zero": "2026-10-10"},
        {"scoring_sessions": ["2026-10-09", "2026-10-07"]},
        {"allowlist": [101]},
        {"mandatory": {101: 0}},
        {"report_source": ""},
        {"risk": {"stress_loss": 0.04}},
        {"gateway_port": 4001},
    ],
)
def test_invalid_manifest_rejected(manifest, change):
    config = manifest.model_dump(mode="json")
    config.update(change)
    with pytest.raises(ValidationError):
        Manifest.model_validate(config)


def test_template_is_intentionally_non_executable():
    with pytest.raises(ValidationError):
        load_manifest("config/practice.template.yaml")


def test_option_expiring_before_final_session_rejected(manifest):
    data = manifest.model_dump(mode="json")
    data["contracts"][1]["expiry"] = "2026-10-08"
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_early_close_has_correct_order_cutoff(manifest):
    config = manifest.model_dump(mode="json")
    config.update(day_zero="2026-11-25", scoring_sessions=["2026-11-27"])
    for c in config["contracts"]:
        if c["kind"] == "OPT":
            c["expiry"] = "2026-12-18"
    m = Manifest.model_validate(config)
    assert m.session_close(date(2026, 11, 27)).astimezone(NY).hour == 13
    assert m.can_trade(datetime(2026, 11, 27, 12, 44, tzinfo=NY), "OPT")
    assert not m.can_trade(datetime(2026, 11, 27, 12, 45, tzinfo=NY), "OPT")


def test_strategy_revision_does_not_erase_score_history(store, manifest):
    ledger = ScoreLedger(store, manifest)
    ledger.import_close(manifest.day_zero, Decimal(1000000), "original", manifest.account)
    changed = manifest.model_copy(update={"strategy": "delta"})
    assert changed.config_hash != manifest.config_hash
    assert changed.contest_id == manifest.contest_id
    assert ScoreLedger(store, changed).closes()[manifest.day_zero] == 1000000


def test_pending_interest_adjustment_preserves_raw_nav_without_claiming_score(manifest, store):
    from decimal import Decimal

    from hedge.config import Manifest
    from hedge.scoring import ScoreLedger

    pending = Manifest.model_validate(
        {**manifest.model_dump(), "confirmed": False, "scoring_rule": "pending_interest_adjustment"}
    )
    ledger = ScoreLedger(store, pending)
    ledger.import_close(pending.day_zero, Decimal("1000278.85"), "Gross IBKR NAV statement", pending.account)
    ledger.import_close(
        pending.scoring_sessions[0], Decimal("1000364.85"), "Gross IBKR NAV statement", pending.account
    )
    assert ledger.closes()[pending.day_zero] == Decimal("1000278.85")
    assert ledger.summary()["total"] is None
    assert ledger.summary()["days"][0]["score"] is None
    assert not ledger.summary()["complete"]
    with pytest.raises(ValueError, match="formula is pending"):
        Manifest.model_validate({**pending.model_dump(), "confirmed": True})


async def test_pending_scoring_blocks_activation_and_provisional_score(manifest, store, snapshot, quotes):
    from hedge.config import Manifest
    from hedge.service import TradingService

    pending = Manifest.model_validate(
        {**manifest.model_dump(), "confirmed": False, "scoring_rule": "pending_interest_adjustment"}
    )
    service = TradingService(pending, store, demo=True)
    service.last_snapshot = snapshot
    service.last_quotes = quotes
    service.ledger.import_close(pending.day_zero, Decimal("999000"), "Gross IBKR statement", pending.account)
    assert service.state()["provisional_score"] is None
    with pytest.raises(ValueError, match="revised scoring formula"):
        await service.control("arm_setup")


def test_interest_benchmark_is_applied_to_whole_prior_nav(store, manifest):
    m = manifest.model_copy(
        update={"scoring_rule": "interest_benchmark", "scoring_annual_rate": Decimal("0.0335")}
    )
    ledger = ScoreLedger(store, m)
    prior = Decimal("1000000")
    target = m.scoring_target(prior)
    assert target == prior * (1 + Decimal("0.0335") / 360)
    ledger.import_close(m.day_zero, prior, "IBKR NAV", m.account)
    ledger.import_close(m.scoring_sessions[0], Decimal("1000000"), "IBKR NAV", m.account)
    score = Decimal(ledger.summary()["days"][0]["score"])
    assert score == target - prior  # flat raw NAV is penalized after discounting
    ledger.import_close(m.scoring_sessions[0], Decimal("1000100"), "IBKR corrected NAV", m.account)
    assert Decimal(ledger.summary()["days"][0]["score"]) == Decimal("1000100") - target
    assert ledger.previous_close(m.scoring_sessions[1]) == Decimal("1000100")
    assert m.scoring_target(ledger.previous_close(m.scoring_sessions[1])) != target


def test_cash_benchmark_path_zero_and_rate_required(store, manifest):
    data = manifest.model_dump()
    data.update(scoring_rule="interest_benchmark", scoring_annual_rate=Decimal("0.036"))
    m = Manifest.model_validate(data)
    ledger = ScoreLedger(store, m)
    nav = Decimal("1000000")
    for day in (m.day_zero, *m.scoring_sessions):
        ledger.import_close(day, nav, "IBKR test NAV", m.account)
        nav = m.scoring_target(nav)
    assert Decimal(ledger.summary()["total"]) == 0
    assert ledger.summary()["complete"]
    data["scoring_annual_rate"] = None
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_actual_daily_rates_no_approximate_fallback_and_corrections(store, manifest):
    m = manifest.model_copy(
        update={"scoring_rule": "actual_daily_cash_rate", "scoring_annual_rate": Decimal("0.0335")}
    )
    ledger = ScoreLedger(store, m)
    ledger.import_close(m.day_zero, Decimal("1000000"), "IBKR close", m.account)
    day = m.scoring_sessions[0]
    ledger.import_close(day, Decimal("1000100"), "IBKR close", m.account)
    assert ledger.target_for(day) is None
    assert ledger.summary()["total"] is None
    assert ledger.summary()["days"][0]["score"] is None
    ledger.import_daily_rate(day, Decimal("0.0001"), "Actual organizer daily rate")
    assert ledger.target_for(day) == Decimal("1000100")
    assert Decimal(ledger.summary()["days"][0]["score"]) == 0
    ledger.import_daily_rate(day, Decimal("0.0002"), "Corrected actual rate")
    assert Decimal(ledger.summary()["days"][0]["score"]) == 100
    assert ledger.target_for(m.scoring_sessions[1]) is None


def test_authorized_forecast_does_not_fabricate_official_rate(store, manifest):
    m = manifest.model_copy(
        update={"scoring_rule": "actual_daily_cash_rate", "scoring_forecast_daily_rate": Decimal("0.0001")}
    )
    ledger = ScoreLedger(store, m)
    ledger.import_close(m.day_zero, Decimal("1000000"), "baseline", m.account)
    day = m.scoring_sessions[0]
    assert ledger.target_for(day) is None
    assert ledger.target_for(day, allow_estimate=True) == Decimal("1000100")
    assert ledger.summary()["days"][0]["score"] is None


def test_operator_can_configure_twenty_percent_delta_cap(manifest):
    from pydantic import ValidationError

    from hedge.config import RiskLimits

    assert RiskLimits(residual_delta=0.2).residual_delta == 0.2
    assert RiskLimits().residual_delta == 0.1
    with pytest.raises(ValidationError):
        RiskLimits(residual_delta=0.21)


def test_fast_paper_policy_retains_risk_uncertainty_and_frequency_floor():
    from pydantic import ValidationError

    from hedge.config import TradingPolicy

    policy = TradingPolicy(stock_seconds=15, option_seconds=120, score_threshold=25)
    assert policy.uncertainty_multiplier == 1.645
    assert policy.option_cutoff_minutes == 15 and policy.stock_cutoff_minutes == 2
    with pytest.raises(ValidationError):
        TradingPolicy(stock_seconds=14)


def test_calendar_interest_benchmark_compounds_weekend_days(store, manifest):
    data = manifest.model_dump()
    data.update(
        day_zero=date(2026, 10, 7),
        scoring_sessions=(date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)),
        scoring_rule="calendar_interest_benchmark",
        scoring_annual_rate=Decimal("0.0344"),
    )
    m = Manifest.model_validate(data)
    assert [m.accrual_days(d) for d in m.scoring_sessions] == [1, 1, 3]
    factor = 1 + Decimal("0.0344") / 360
    prior = Decimal("1000000")
    assert m.scoring_target(prior, date(2026, 10, 9)) == prior * factor
    assert m.scoring_target(prior, date(2026, 10, 12)) == prior * factor**3
    ledger = ScoreLedger(store, m)
    ledger.import_close(m.day_zero, prior, "IBKR test NAV", m.account)
    nav = prior
    for day in m.scoring_sessions:  # an all-cash account scores zero
        nav = m.scoring_target(nav, day)
        ledger.import_close(day, nav, "IBKR test NAV", m.account)
    assert abs(Decimal(ledger.summary()["total"])) < Decimal("1e-6")
    data["scoring_annual_rate"] = None
    with pytest.raises(ValidationError):
        Manifest.model_validate(data)

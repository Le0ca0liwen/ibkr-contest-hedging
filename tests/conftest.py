from datetime import date, datetime

import pytest

from hedge.config import NY, Manifest
from hedge.demo import demo_quotes
from hedge.models import Contract, PortfolioSnapshot, Position
from hedge.storage import Store


@pytest.fixture(autouse=True)
def gemini_unless_a_test_selects_claude(monkeypatch):
    """The app defaults to Claude Sonnet 5.5; the older AI tests were written against Gemini and keep
    it explicitly. Claude tests pass model="claude-sonnet-5-5"; no real key ever leaks in."""
    monkeypatch.setenv("HEDGE_AI_MODEL", "gemini-3.8-flash")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manifest():
    stock = Contract(con_id=10, symbol="BA", kind="STK")
    specs = [
        (101, 195, "C"),
        (102, 195, "P"),
        (103, 190, "C"),
        (104, 200, "C"),
        (105, 190, "P"),
        (106, 200, "P"),
    ]
    contracts = (
        stock,
        *(
            Contract(
                con_id=i,
                symbol="BA",
                kind="OPT",
                multiplier=100,
                expiry=date(2026, 10, 16),
                strike=strike,
                right=right,
                underlying_id=10,
            )
            for i, strike, right in specs
        ),
    )
    return Manifest(
        name="Test contest",
        account="DU_TEST",
        day_zero=date(2026, 10, 6),
        scoring_sessions=(date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)),
        valuation_convention="IBKR reported closing NAV",
        report_source="Instructor IBKR report",
        contracts=contracts,
        mandatory={101: 25, 102: 25},
        allowlist=(10, 103, 104, 105, 106),
        confirmed=True,
    )


@pytest.fixture
def now():
    return datetime(2026, 10, 7, 10, tzinfo=NY)


@pytest.fixture
def quotes(manifest, now):
    return {i: q.model_copy(update={"data_type": "live"}) for i, q in demo_quotes(manifest, now).items()}


@pytest.fixture
def snapshot(manifest, now):
    return PortfolioSnapshot(
        account=manifest.account,
        at=now,
        account_at=now,
        nav="1000000",
        cash="950000",
        excess_liquidity="800000",
        initial_margin="200000",
        positions=tuple(Position(con_id=i, quantity=q) for i, q in manifest.mandatory.items()),
    )


@pytest.fixture
def store(tmp_path):
    result = Store(f"sqlite:///{tmp_path}/test.db")
    result.initialize()
    yield result
    result.close()

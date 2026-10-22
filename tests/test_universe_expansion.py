from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hedge.models import Contract
from hedge.optimizer import HedgeOptimizer
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
from hedge.universe_discovery import qualify_stock_universe


class DiscoveryBroker:
    def __init__(self):
        self.disconnected = False
        self.connection = None

    async def connectAsync(self, *args, **kwargs):
        self.connection = kwargs

    def managedAccounts(self):
        return ["DU_TEST"]

    async def reqContractDetailsAsync(self, native):
        return [
            SimpleNamespace(
                contract=SimpleNamespace(symbol=native.symbol, secType="STK", currency="USD", conId=200)
            )
        ]

    def disconnect(self):
        self.disconnected = True


async def test_stock_discovery_is_readonly_and_preserves_mandates(manifest):
    broker = DiscoveryBroker()
    expanded = await qualify_stock_universe(manifest, ["SPY", "BA"], broker)
    assert broker.connection["readonly"] and broker.disconnected
    assert expanded.mandatory == manifest.mandatory
    assert expanded.contract_map[200].symbol == "SPY"
    assert 200 in expanded.allowlist
    with pytest.raises(ValueError):
        await qualify_stock_universe(manifest, ["../../bad"], broker)


def joint_history(path, now, future=False):
    days = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=70)
    rng = np.random.default_rng(31)
    market = rng.normal(0, 0.01, len(days))
    frames = []
    for symbol, beta in [("BA", 1.4), ("SPY", 1.0)]:
        prices = 100 * np.exp(np.cumsum(beta * market + rng.normal(0, 0.002, len(days))))
        frames.extend(
            {"date": str(d.date()), "symbol": symbol, "close": p} for d, p in zip(days, prices, strict=True)
        )
        if future:
            frames.append({"date": str(now.date()), "symbol": symbol, "close": 999999})
    pd.DataFrame(frames).to_csv(path, index=False)


def test_joint_daily_scenarios_deterministic_no_future_or_missing_history(
    manifest, snapshot, quotes, now, tmp_path
):
    path = tmp_path / "stocks.csv"
    joint_history(path, now)
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={
            "contracts": (*manifest.contracts, spy),
            "allowlist": (*manifest.allowlist, 200),
            "daily_stock_history": str(path),
        }
    )
    quotes = {
        **quotes,
        200: quotes[10].model_copy(
            update={"con_id": 200, "spot": 450, "bid": 449.99, "ask": 450.01, "shortable_shares": None}
        ),
    }
    ids = screen_options(m, quotes, snapshot.quantities())
    assert 200 in ids
    engine = ScenarioEngine(m)
    first = engine.build(snapshot, quotes, now, ids)
    assert first.identifier == engine.build(snapshot, quotes, now, ids).identifier
    assert "shrunk covariance" in first.source
    joint_history(path, now, future=True)
    assert ScenarioEngine(m).build(snapshot, quotes, now, ids).identifier == first.identifier
    target = HedgeOptimizer(m).solve(snapshot, quotes, first, m.initial_nav)
    assert target.status == "OPTIMAL"
    assert target.quantities.get(200, 0) >= 0  # no invented stock borrow
    assert all(target.quantities[i] == q for i, q in m.mandatory.items())
    frame = pd.read_csv(path)
    frame[frame.symbol == "BA"].to_csv(path, index=False)
    with pytest.raises(ValueError, match="Missing joint stock history"):
        ScenarioEngine(m).build(snapshot, quotes, now, ids)


def test_cross_underlying_activation_requires_joint_history(manifest, snapshot, quotes, now):
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, spy), "allowlist": (*manifest.allowlist, 200)}
    )
    quotes = {**quotes, 200: quotes[10].model_copy(update={"con_id": 200})}
    with pytest.raises(ValueError, match="Joint historical coverage required"):
        ScenarioEngine(m).build(snapshot, quotes, now)


def test_related_stocks_use_measured_joint_signs_and_combined_limits(
    manifest, snapshot, quotes, now, tmp_path
):
    """A negative relationship enters scenarios without granting unrestricted trades."""
    additions = (
        Contract(con_id=200, symbol="UAL", kind="STK"),
        Contract(con_id=201, symbol="GE", kind="STK"),
        Contract(con_id=202, symbol="XLE", kind="STK"),
    )
    rng = np.random.default_rng(42)
    days = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=70)
    factor = rng.normal(0, 0.012, len(days))
    records = []
    for symbol, beta in (("BA", 1.0), ("UAL", 0.9), ("GE", 1.1), ("XLE", -0.8)):
        prices = 100 * np.exp(np.cumsum(beta * factor + rng.normal(0, 0.002, len(days))))
        records.extend(
            {"date": str(day.date()), "symbol": symbol, "close": price}
            for day, price in zip(days, prices, strict=True)
        )
    path = tmp_path / "related.csv"
    pd.DataFrame(records).to_csv(path, index=False)
    m = type(manifest).model_validate(
        {
            **manifest.model_dump(),
            "contracts": (*manifest.contracts, *additions),
            "allowlist": (*manifest.allowlist, *(c.con_id for c in additions)),
            "daily_stock_history": str(path),
        }
    )
    qs = dict(quotes)
    for contract, price in zip(additions, (100, 300, 90), strict=True):
        qs[contract.con_id] = quotes[10].model_copy(
            update={
                "con_id": contract.con_id,
                "spot": price,
                "bid": price - 0.01,
                "ask": price + 0.01,
                "shortable_shares": None,
            }
        )
    engine = ScenarioEngine(m)
    covariance = engine.daily_covariance(now, ["BA", "UAL", "GE", "XLE"])
    assert covariance[0, 1] > 0 and covariance[0, 2] > 0 and covariance[0, 3] < 0
    ids = screen_options(m, qs, snapshot.quantities())
    scenarios = engine.build(snapshot, qs, now, ids)
    assert all(c.con_id in scenarios.ids for c in additions)
    ba = scenarios.changes[:, scenarios.ids.index(10)]
    energy = scenarios.changes[:, scenarios.ids.index(202)]
    assert np.corrcoef(ba, energy)[0, 1] < 0
    target = HedgeOptimizer(m).solve(snapshot, qs, scenarios, m.initial_nav)
    assert target.status == "OPTIMAL"
    assert all(target.quantities[i] == amount for i, amount in m.mandatory.items())
    assert all(target.quantities.get(c.con_id, 0) >= 0 for c in additions)
    assert RiskGate(m).validate(snapshot, qs, scenarios, target.quantities, now, target.estimated_cost).passed


class ChainBroker(DiscoveryBroker):
    async def reqSecDefOptParamsAsync(self, symbol, exchange, kind, con_id):
        return [
            SimpleNamespace(
                exchange="SMART",
                tradingClass=symbol,
                multiplier="100",
                expirations=[
                    "20261016",
                    "20261104",
                    "20261105",
                    "20261119",
                    "20261120",
                    "20261123",
                    "20261218",
                ],
                strikes=[*range(35, 61), *range(120, 171, 5), *range(750, 791, 5)],
            )
        ]

    async def reqContractDetailsAsync(self, native):
        if native.secType == "STK":
            con_id = {"SPCX": 10, "RKLB": 20, "SPY": 30, "IVV": 40}[native.symbol]
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        symbol=native.symbol, secType="STK", currency="USD", conId=con_id
                    )
                )
            ]
        con_id = 1000 + len(self.seen)
        self.seen.append((native.symbol, native.lastTradeDateOrContractMonth, native.strike, native.right))
        underlying = {"SPCX": 10, "RKLB": 20, "SPY": 30}[native.symbol]
        contract = SimpleNamespace(
            symbol=native.symbol,
            secType="OPT",
            currency="USD",
            conId=con_id,
            lastTradeDateOrContractMonth=native.lastTradeDateOrContractMonth,
            strike=native.strike,
            right=native.right,
            multiplier="100",
            tradingClass=native.symbol,
        )
        return [SimpleNamespace(contract=contract, underConId=underlying)]


async def test_option_chain_discovery_skips_forbidden_and_short_expiries():
    from datetime import date

    from hedge.config import Manifest

    expiry = date(2026, 11, 20)
    stocks = [Contract(con_id=i, symbol=s, kind="STK") for i, s in [(10, "SPCX"), (20, "RKLB"), (30, "SPY")]]
    legs = [
        (101, 10, 130, "C", 25),
        (102, 10, 145, "C", -50),
        (103, 10, 160, "C", 25),
        (104, 20, 45, "P", -25),
        (105, 20, 55, "P", 25),
        (106, 30, 770, "C", 25),
        (107, 30, 770, "P", 25),
    ]
    symbols = {10: "SPCX", 20: "RKLB", 30: "SPY"}
    options = [
        Contract(
            con_id=i,
            symbol=symbols[u],
            kind="OPT",
            multiplier=100,
            expiry=expiry,
            strike=k,
            right=r,
            underlying_id=u,
        )
        for i, u, k, r, _ in legs
    ]
    m = Manifest(
        name="Contest",
        account="DU_TEST",
        day_zero=date(2026, 10, 21),
        scoring_sessions=(date(2026, 10, 22), date(2026, 11, 4)),
        valuation_convention="IBKR NAV",
        report_source="IBKR statement",
        contracts=(*stocks, *options),
        mandatory={i: q for i, *_, q in legs},
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=expiry,
        universe="open",
    )
    broker = ChainBroker()
    broker.seen = []
    expanded = await qualify_stock_universe(
        m, ["IVV"], broker, option_symbols=["RKLB", "SPY"], expiries=2, strikes=3
    )
    assert broker.connection["readonly"] and broker.disconnected
    added = [c for c in expanded.contracts if c.con_id >= 1000]
    # Expiries nearest the contest expiry on both sides, never the days right after the contest.
    assert {c.expiry for c in added if c.symbol == "RKLB"} == {expiry, date(2026, 11, 19)}
    assert {c.expiry for c in added if c.symbol == "SPY"} == {date(2026, 11, 19), date(2026, 11, 23)}
    assert not any(e[1] == "20261120" and e[0] == "SPY" for e in broker.seen)  # never requested
    assert not any(c.symbol == "RKLB" and c.right == "C" and c.expiry == expiry for c in added)
    assert not any(c.symbol == "SPY" and c.expiry == expiry for c in added)
    assert {c.con_id for c in added} <= set(expanded.allowlist)
    assert 40 in expanded.allowlist and 30 not in expanded.allowlist
    assert expanded.mandatory == m.mandatory


def test_daily_history_reloads_and_stale_history_waits(manifest, now, tmp_path):
    import os
    from datetime import timedelta

    from hedge.continuity import DataUnavailable

    path = tmp_path / "stocks.csv"
    joint_history(path, now)
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, spy), "daily_stock_history": str(path)}
    )
    engine = ScenarioEngine(m)
    assert engine.daily_covariance(now, ("BA", "SPY")).shape == (2, 2)
    later = now + timedelta(days=10)
    with pytest.raises(DataUnavailable, match="stale"):
        engine.daily_covariance(later, ("BA", "SPY"))
    good = engine.daily_history.copy()
    pd.DataFrame({"date": ["2026-10-20"], "symbol": ["BA"], "close": [-1]}).to_csv(path, index=False)
    os.utime(path, ns=(engine.daily_mtime + 10**9, engine.daily_mtime + 10**9))
    engine.refresh_daily_history()  # a bad fetch never replaces good history
    assert engine.daily_history.equals(good)
    joint_history(path, later)  # the operator re-runs the daily fetch
    os.utime(path, ns=(engine.daily_mtime + 10**9, engine.daily_mtime + 10**9))
    engine.refresh_daily_history()
    assert engine.daily_covariance(later, ("BA", "SPY")).shape == (2, 2)


def test_screening_uses_neighbouring_expiries_when_contest_expiry_is_forbidden(manifest, quotes):
    from datetime import date

    base = manifest.contract_map[101]
    neighbours = [
        base.model_copy(update={"con_id": 300 + n, "expiry": e, "strike": k})
        for n, (e, k) in enumerate(
            [(date(2026, 10, 15), 195), (date(2026, 10, 15), 200), (date(2026, 10, 19), 195)]
        )
    ]
    m = manifest.model_copy(
        update={
            "contracts": (*manifest.contracts, *neighbours),
            "allowlist": (10, *(c.con_id for c in neighbours)),
        }
    )
    quotes = {**quotes, **{c.con_id: quotes[101].model_copy(update={"con_id": c.con_id}) for c in neighbours}}
    ids = screen_options(m, quotes, {})
    # No same-expiry candidates are allowlisted, so both neighbouring expiries are screened.
    assert {300, 301, 302} <= set(ids)


async def test_prepare_contest_qualifies_signed_legs_from_final_strikes(tmp_path):
    from pathlib import Path

    import yaml

    from hedge.universe_discovery import prepare_contest

    template = yaml.safe_load(Path("config/contest.template.yaml").read_text())
    (tmp_path / "daily").mkdir()
    template["daily_stock_history"] = str(tmp_path / "daily" / "contest.csv")
    broker = ChainBroker()
    broker.seen = []
    m = await prepare_contest(
        template, None, {"SPCX": [160, 130, 145], "RKLB": [55, 45], "SPY": [770]}, "IBKR statement", ib=broker
    )
    assert broker.connection["readonly"] and broker.disconnected and m.account == "DU_TEST"
    signed = sorted(
        (c.symbol, c.strike, c.right, m.mandatory[c.con_id]) for c in m.contracts if c.con_id in m.mandatory
    )
    assert signed == [
        ("RKLB", 45, "P", -25),
        ("RKLB", 55, "P", 25),
        ("SPCX", 130, "C", 25),
        ("SPCX", 145, "C", -50),
        ("SPCX", 160, "C", 25),
        ("SPY", 770, "C", 25),
        ("SPY", 770, "P", 25),
    ]
    assert m.market_data_provider == "moomoo" and not m.confirmed
    assert set(m.allowlist) == {10, 20}  # SPY shares stay quote-only under the open universe


def test_neighbouring_expiries_alternate_sides():
    from datetime import date

    from hedge.universe import neighbouring_expiries

    days = [date(2026, 11, d) for d in (13, 17, 18, 19, 20, 23, 24, 27)]
    order = neighbouring_expiries(days, date(2026, 11, 20))
    assert order[:4] == [date(2026, 11, 19), date(2026, 11, 23), date(2026, 11, 18), date(2026, 11, 24)]
    weekly = neighbouring_expiries([date(2026, 11, d) for d in (13, 20, 27)], date(2026, 11, 20))
    assert weekly == [date(2026, 11, 27), date(2026, 11, 13)]  # tie prefers the later expiry


def test_missing_daily_history_file_waits_then_loads(manifest, snapshot, quotes, now, tmp_path):
    from hedge.continuity import DataUnavailable

    path = tmp_path / "later.csv"
    spy = Contract(con_id=200, symbol="SPY", kind="STK")
    m = manifest.model_copy(
        update={"contracts": (*manifest.contracts, spy), "daily_stock_history": str(path)}
    )
    engine = ScenarioEngine(m)  # the service must start before the first fetch
    with pytest.raises(DataUnavailable, match="not found"):
        engine.build(snapshot, quotes, now)
    joint_history(path, now)
    engine.refresh_daily_history()
    assert engine.daily_covariance(now, ("BA", "SPY")).shape == (2, 2)


async def test_discovery_skips_strikes_not_listed_at_an_expiry():
    from datetime import date

    from ib_async import RequestError

    from hedge.config import Manifest

    class SparseBroker(ChainBroker):
        async def reqContractDetailsAsync(self, native):
            if (
                native.secType == "OPT"
                and native.strike == 770
                and native.lastTradeDateOrContractMonth == "20261119"
            ):
                raise RequestError(1, 200, "No security definition has been found for the request")
            return await super().reqContractDetailsAsync(native)

    expiry = date(2026, 11, 20)
    stocks = [Contract(con_id=i, symbol=s, kind="STK") for i, s in [(10, "SPCX"), (20, "RKLB"), (30, "SPY")]]
    legs = [
        (101, 10, 130, "C", 25),
        (102, 10, 145, "C", -50),
        (103, 10, 160, "C", 25),
        (104, 20, 45, "P", -25),
        (105, 20, 55, "P", 25),
        (106, 30, 770, "C", 25),
        (107, 30, 770, "P", 25),
    ]
    symbols = {10: "SPCX", 20: "RKLB", 30: "SPY"}
    options = [
        Contract(
            con_id=i,
            symbol=symbols[u],
            kind="OPT",
            multiplier=100,
            expiry=expiry,
            strike=k,
            right=r,
            underlying_id=u,
        )
        for i, u, k, r, _ in legs
    ]
    m = Manifest(
        name="Contest",
        account="DU_TEST",
        day_zero=date(2026, 10, 21),
        scoring_sessions=(date(2026, 10, 22), date(2026, 11, 4)),
        valuation_convention="IBKR NAV",
        report_source="IBKR statement",
        contracts=(*stocks, *options),
        mandatory={i: q for i, *_, q in legs},
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=expiry,
        universe="open",
    )
    broker = SparseBroker()
    broker.seen = []
    expanded = await qualify_stock_universe(m, [], broker, option_symbols=["SPY"], expiries=2, strikes=3)
    added = [c for c in expanded.contracts if c.con_id >= 1000]
    nov19 = {c.strike for c in added if c.expiry == date(2026, 11, 19)}
    assert 770 not in nov19 and len(nov19) == 3  # walked past the unlisted strike to the next one


async def test_voo_and_ivv_are_added_as_tradable_hedges_with_a_borrow_allowance(monkeypatch):
    from datetime import date
    from pathlib import Path

    from hedge.config import Manifest

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "ops"))
    from ops.add_hedge_stocks import add

    expiry = date(2026, 11, 20)
    stocks = [Contract(con_id=i, symbol=s, kind="STK") for i, s in [(10, "SPCX"), (20, "RKLB"), (30, "SPY")]]
    legs = [
        (101, 10, 130, "C", 25),
        (102, 10, 145, "C", -50),
        (103, 10, 160, "C", 25),
        (104, 20, 45, "P", -25),
        (105, 20, 55, "P", 25),
        (106, 30, 770, "C", 25),
        (107, 30, 770, "P", 25),
    ]
    symbols = {10: "SPCX", 20: "RKLB", 30: "SPY"}
    options = [
        Contract(
            con_id=i,
            symbol=symbols[u],
            kind="OPT",
            multiplier=100,
            expiry=expiry,
            strike=k,
            right=r,
            underlying_id=u,
        )
        for i, u, k, r, _ in legs
    ]
    m = Manifest(
        name="Contest",
        account="DU_TEST",
        day_zero=date(2026, 10, 21),
        scoring_sessions=(date(2026, 10, 22), date(2026, 11, 4)),
        valuation_convention="IBKR NAV",
        report_source="IBKR statement",
        contracts=(*stocks, *options),
        mandatory={i: q for i, *_, q in legs},
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=expiry,
        universe="allowlist",
        allowlist=(10, 20),
    )

    class Broker(DiscoveryBroker):
        async def reqContractDetailsAsync(self, native):
            con_id = {"VOO": 136155102, "IVV": 8991352}[native.symbol]
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        symbol=native.symbol, secType="STK", currency="USD", conId=con_id
                    )
                )
            ]

    qualified = await qualify_stock_universe(m, ["VOO", "IVV"], Broker())
    updated, added = add(m, qualified, 300, "IBKR paper: easy to borrow (operator)")
    assert added == ["IVV", "VOO"]
    assert {136155102, 8991352} <= set(
        updated.allowlist
    ) and 30 not in updated.allowlist  # SPY shares stay banned
    assert updated.stock_borrow == {"VOO": 300, "IVV": 300}
    assert updated.stock_borrow_source == "IBKR paper: easy to borrow (operator)"
    assert updated.mandatory == m.mandatory and updated.config_hash != m.config_hash

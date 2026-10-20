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
                expirations=["20261016", "20261120", "20261218", "20270115"],
                strikes=[90, 95, 100, 105, 110, 115, 120],
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
        (101, 10, 95, "C", 25),
        (102, 10, 100, "C", -50),
        (103, 10, 105, "C", 25),
        (104, 20, 95, "P", -25),
        (105, 20, 105, "P", 25),
        (106, 30, 100, "C", 25),
        (107, 30, 100, "P", 25),
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
    assert added and all(c.expiry in (expiry, date(2026, 12, 18)) for c in added)
    assert not any(c.symbol == "RKLB" and c.right == "C" and c.expiry == expiry for c in added)
    assert not any(c.symbol == "SPY" and c.expiry == expiry for c in added)
    assert {c.con_id for c in added} <= set(expanded.allowlist)
    assert 40 in expanded.allowlist and 30 not in expanded.allowlist
    assert expanded.mandatory == m.mandatory

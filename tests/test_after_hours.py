"""Contest post-market session (16:00-20:00 ET): shares keep hedging, options are valued from
their live underlying and never traded, the forecast runs to the 20:00 scored close."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hedge.config import NY
from hedge.demo import demo_manifest
from hedge.models import Position
from hedge.moomoo_data import MoomooData, code_for
from hedge.scenarios import POST_MARKET_VARIANCE, trading_variance


def contest(manifest):
    return manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})


def at(day, hour, minute=0, second=0):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute, second=second)


def test_only_the_closing_burner_trades_shares_in_the_post_market_session(manifest):
    """The statement marks every position at the close (Oct 23): a post-market hedge would only
    book its price versus that mark plus fees, so the hedger stops at the close."""
    m, day = contest(manifest), manifest.scoring_sessions[0]
    assert not m.can_trade(at(day, 17), "STK") and not m.can_trade(at(day, 17), "OPT")
    burner = {"post_market": True}
    assert m.can_trade(at(day, 17), "STK", **burner) and not m.can_trade(at(day, 17), "OPT", **burner)
    assert m.can_trade(at(day, 19, 57, 59), "STK", **burner) and not m.can_trade(
        at(day, 19, 58), "STK", **burner
    )
    assert not m.can_trade(at(day, 15, 59), "STK", **burner)  # the regular close's own cutoff still applies
    assert not m.can_trade(at(day, 20, 30), "STK", **burner)
    assert not m.can_trade(at(day, 17), "STK", setup=True, post_market=True)
    assert m.can_trade(at(day, 5), "STK")  # pre-market hedging continues
    assert m.post_market(at(day, 16)) == day and m.post_market(at(day, 20)) is None
    assert not manifest.can_trade(at(day, 17), "STK") and manifest.post_market(at(day, 17)) is None


def test_the_forecast_runs_to_the_16_00_statement_close(manifest):
    """The statement marks at the regular close (Oct 23); post-market trades book until 20:00."""
    m, day = contest(manifest), manifest.scoring_sessions[0]
    assert m.scoring_close(day) == at(day, 16) and m.trading_end(day) == at(day, 20)
    assert m.next_close(at(day, 10)) == at(day, 16)
    assert m.next_close(at(day, 17)) == at(manifest.scoring_sessions[1], 16)
    last = m.scoring_sessions[-1]
    assert m.next_close(at(last, 17)) == at(last, 20)  # the final statement still books until 20:00
    assert m.next_close(at(last, 20, 1)) is None
    assert m.option_close(day, "SPY") == at(day, 16, 15) and m.option_close(day, "RKLB") == at(day, 16)
    assert manifest.next_close(at(day, 10)) == at(day, 16)  # practice: the regular close


def test_post_market_variance_accrues_evenly_and_whole_gaps_are_unchanged(manifest):
    day, following = manifest.scoring_sessions[:2]
    assert trading_variance(at(day, 17), at(day, 20)) == pytest.approx(0.75 * POST_MARKET_VARIANCE)
    assert trading_variance(at(day, 10), at(day, 20)) == pytest.approx(6 / 6.5 + POST_MARKET_VARIANCE)
    # A whole close -> reopen gap still counts the full overnight variance.
    whole = trading_variance(at(day, 15), at(following, 10))
    assert whole == pytest.approx(1 / 6.5 + 0.30 + 0.5 / 6.5)
    # From the 20:00 scored close to the next open: only the rest of the gap.
    assert trading_variance(at(day, 20), at(following, 9, 45)) == pytest.approx(
        0.30 - POST_MARKET_VARIANCE + 0.25 / 6.5
    )


def feed(m, now, raw_overrides=None):
    """A Moomoo snapshot: every book pushed twice, current to `now`, full rights."""
    raw = {
        "purpose": "market_data_only",
        "execution_enabled": False,
        "received_at": now.isoformat(),
        "quote_server_connected": True,
        "rights": {"us_qot_right": "LV3", "us_option_qot_right": "LV1"},
        "identities": {},
        "securities": {},
    }
    from hedge.valuation import ValuationEngine

    for c in m.contracts:
        price = ValuationEngine(m).price(c, 195, 0.3, now) if c.kind == "OPT" else 195
        raw["securities"][code_for(c)] = {
            "quote": [
                {
                    "code": code_for(c),
                    "data_date": str(now.date()),
                    "data_time": "15:59:00",
                    "strike_price": c.strike,
                    "contract_multiplier": c.multiplier,
                    "implied_volatility": 30,
                }
            ],
            "order_book": {"Bid": [[price - 0.05, 10]], "Ask": [[price + 0.05, 10]]},
            "book_push_at": now.isoformat(),
            "book_push_count": 2,
        }
        if c.kind == "OPT":
            raw["identities"][code_for(c)] = {
                "stock_owner": f"US.{c.symbol}",
                "strike_time": str(c.expiry),
                "option_type": "CALL" if c.right == "C" else "PUT",
                "strike_price": c.strike,
            }
    raw.update(raw_overrides or {})
    return raw


def test_after_hours_options_are_valued_from_the_live_underlying(tmp_path):
    base = demo_manifest()
    m = contest(base)
    now = at(m.scoring_sessions[0], 17)
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(feed(m, now)))
    quotes = MoomooData(m, path).quotes(now)
    stock = next(c for c in m.contracts if c.kind == "STK")
    option = next(c for c in m.contracts if c.kind == "OPT")
    assert quotes[stock.con_id].data_type == "live"  # the after-hours stock book counts
    q = quotes[option.con_id]
    assert q.data_type == "model" and q.observed_at == now and q.bid_size == q.ask_size == 0
    assert q.iv == pytest.approx(0.30) and q.mid == pytest.approx(q.model_price, abs=1e-9)
    assert q.ask - q.bid == pytest.approx(0.10)  # the day's spread around the model value
    # Practice configuration: nothing is live or modeled after the close.
    path.write_text(json.dumps(feed(base, now)))
    assert all(q.data_type == "frozen" for q in MoomooData(base, path).quotes(now).values())
    # A stale underlying book: no after-hours valuation.
    stale = feed(m, now)
    stale["securities"][code_for(stock)]["book_push_at"] = (now - timedelta(seconds=301)).isoformat()
    path.write_text(json.dumps(stale))
    assert MoomooData(m, path).quotes(now)[option.con_id].data_type == "frozen"


def test_the_risk_gate_accepts_a_held_modeled_option_but_never_trades_one(manifest, snapshot, quotes):
    from hedge.risk import RiskGate
    from hedge.scenarios import ScenarioEngine

    m = contest(manifest)
    now = at(m.scoring_sessions[0], 17)
    fresh = {
        i: q.model_copy(
            update={"observed_at": now, "greek_at": now, "data_type": "model" if i != 10 else "live"}
        )
        for i, q in quotes.items()
    }
    snap = snapshot.model_copy(update={"at": now, "account_at": now})
    scenarios = ScenarioEngine(m).build(snap, fresh, now)
    gate = RiskGate(m)
    held = gate.validate(snap, fresh, scenarios, snap.quantities(), now)
    assert not any(r.startswith("Non-live quote") for r in held.reasons), held.reasons
    traded = gate.validate(snap, fresh, scenarios, {**snap.quantities(), 103: 1}, now)
    assert any(
        r.startswith(("Non-live quote: 103", "Outside permitted order window: 103")) for r in traded.reasons
    )


def test_after_hours_share_orders_are_limits_flagged_outside_regular_hours(manifest, monkeypatch):
    import hedge.broker as broker_module
    from hedge.broker import IBKRBroker
    from hedge.models import OrderRequest

    m = contest(manifest)
    broker = IBKRBroker(m, asyncio.Queue())
    placed = []
    broker.ib = SimpleNamespace(
        isConnected=lambda: True,
        placeOrder=lambda contract, order: (
            placed.append(order) or SimpleNamespace(order=SimpleNamespace(orderId=1, permId=2))
        ),
    )
    broker.readonly, broker.qualified, broker.reconciled = False, True, True
    broker.native = {10: object()}

    def request(order_type="LMT"):
        return OrderRequest(
            intent_id="x",
            con_id=10,
            signed_quantity=100,
            limit_price=Decimal("195.00"),
            account=m.account,
            snapshot_id="s",
            decision_id="d",
            order_type=order_type,
        )

    day = m.scoring_sessions[0]
    monkeypatch.setattr(broker_module, "utcnow", lambda: at(day, 17))
    asyncio.run(broker.submit(request(), 1))
    assert placed[-1].outsideRth is True
    with pytest.raises(ValueError, match="Only limit orders"):
        asyncio.run(broker.submit(request("MKT"), 2))
    monkeypatch.setattr(broker_module, "utcnow", lambda: at(day, 11))
    asyncio.run(broker.submit(request(), 3))
    assert placed[-1].outsideRth is False


def test_after_hours_urgency_uses_the_post_market_cutoff_and_never_a_market_order(manifest, quotes):
    from hedge.order_policy import choose_order, urgency_for

    m = contest(manifest)
    day = m.scoring_sessions[0]
    stock = m.contract_map[10]
    assert urgency_for(m, stock, at(day, 17)) == "NORMAL"
    assert urgency_for(m, stock, at(day, 19, 57)) == "DEADLINE"
    book = quotes[10].model_copy(
        update={
            "bid": 195.0,
            "ask": 195.01,
            "bid_size": 10_000,
            "ask_size": 10_000,
            "observed_at": at(day, 17),
        }
    )
    broker = SimpleNamespace(price_tick=lambda con_id, value: 0.01)
    order = choose_order(m, broker, stock, book, 10, at(day, 17), corrective=True)
    assert order["order_type"] == "LMT"


@pytest.mark.parametrize("session, hour", [(0, 17), (1, 7)])  # post-market, pre-market
async def test_paper_trading_starts_after_hours_and_plans_shares_only(manifest, store, quotes, session, hour):
    from hedge.config import Manifest
    from hedge.scenarios import ScenarioEngine
    from hedge.service import TradingService

    m = contest(Manifest.model_validate({**manifest.model_dump(), "autostart": "paper"}))
    now = at(m.scoring_sessions[session], hour)
    service = TradingService(m, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {
        i: q.model_copy(update={"data_type": "synthetic", "observed_at": now, "greek_at": now})
        for i, q in quotes.items()
    }
    service.broker.update_quotes(fresh, now)
    service.broker.now = now
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), fresh
    service.last_scenarios = ScenarioEngine(m).build(service.last_snapshot, fresh, now)
    service.ledger.import_close(m.day_zero, m.initial_nav, "fixture", m.account)
    service.ledger.import_close(m.scoring_sessions[0], m.initial_nav, "fixture", m.account)
    service.restore_autostart()
    service._autostart_step([])
    snapshot = service.broker.snapshot()
    service.last_risk = service.gate.validate(
        snapshot, fresh, service.last_scenarios, snapshot.quantities(), now
    )
    assert service.last_risk.passed, service.last_risk.reasons
    await service._resume_after_data_wait()
    assert service.mode == "PAPER", service.health
    await service.optimize()
    target = service.last_target
    assert target is not None
    changed = {i for i, q in target.quantities.items() if q != snapshot.quantities().get(i, 0)}
    assert all(m.contract_map[i].kind == "STK" for i in changed), changed


def test_the_last_day_keeps_trading_until_the_scored_close(manifest, store):
    from hedge.service import TradingService

    m = contest(manifest)
    service = TradingService(m, store, demo=True)
    last = m.scoring_sessions[-1]
    service.now = lambda: at(last, 17)
    assert not service.contest_over()
    service.now = lambda: at(last, 20)
    assert service.contest_over()
    practice = TradingService(manifest, store, demo=True)
    practice.now = lambda: at(last, 16)
    assert practice.contest_over()


def test_a_close_cannot_be_entered_before_the_post_market_session_ends(manifest, store):
    import re

    from fastapi.testclient import TestClient

    from hedge.service import TradingService
    from hedge.webapp import create_app

    m = contest(manifest)
    service = TradingService(m, store, demo=True)
    client = TestClient(create_app(service))
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    day = m.scoring_sessions[0]

    def post():
        return client.post(
            "/api/closes",
            json={"date": str(day), "nav": str(m.initial_nav), "source": "IBKR", "account": m.account},
            headers={"X-Control-Token": token},
        )

    service.now = lambda: at(day, 17)
    refused = post()
    assert refused.status_code in (400, 409, 422) and "until 20:00 ET" in refused.text
    service.now = lambda: at(day, 20, 30)
    assert post().status_code == 200


def test_the_day_report_counts_the_trades_booked_to_each_statement(manifest, store):
    from hedge.service import TradingService

    m = contest(manifest)
    service = TradingService(m, store, demo=True)
    day = m.scoring_sessions[1]
    assert service.day_reports._window(day) == (at(m.scoring_sessions[0], 20), at(day, 20))


def test_the_after_hours_model_mid_is_the_model_price_even_after_a_wide_day_book(tmp_path):
    m = contest(demo_manifest())
    now = at(m.scoring_sessions[0], 17)
    raw = feed(m, now)
    option = next(c for c in m.contracts if c.kind == "OPT" and c.right == "C" and c.strike > 195)
    raw["securities"][code_for(option)]["order_book"] = {"Bid": [[0.01, 1]], "Ask": [[40.0, 1]]}
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(raw))
    q = MoomooData(m, path).quotes(now)[option.con_id]
    assert q.mid == pytest.approx(q.model_price) and q.bid >= 0


def test_the_after_hours_margin_preview_matches_the_order_flag(manifest, monkeypatch):
    import hedge.broker as broker_module
    from hedge.broker import IBKRBroker
    from hedge.models import OrderRequest

    m = contest(manifest)
    broker = IBKRBroker(m, asyncio.Queue())
    previews = []

    async def what_if(contract, order):
        previews.append(order)
        return SimpleNamespace()

    broker.ib = SimpleNamespace(whatIfOrderAsync=what_if)
    broker.native = {10: object()}
    request = OrderRequest(
        intent_id="x",
        con_id=10,
        signed_quantity=100,
        limit_price=Decimal("195.00"),
        account=m.account,
        snapshot_id="s",
        decision_id="d",
    )
    monkeypatch.setattr(broker_module, "utcnow", lambda: at(m.scoring_sessions[0], 17))
    asyncio.run(broker.margin_preview(request))
    assert previews[-1].outsideRth is True


def test_after_hours_a_quiet_book_values_a_held_stock_but_a_trade_needs_a_fresh_one(
    manifest, snapshot, quotes
):
    from hedge.risk import RiskGate
    from hedge.scenarios import ScenarioEngine

    m = contest(manifest)
    now = at(m.scoring_sessions[0], 17)
    quiet = {
        i: q.model_copy(
            update={
                "observed_at": now - timedelta(seconds=60),
                "greek_at": now - timedelta(seconds=60),
                "data_type": "model" if m.contract_map[i].kind == "OPT" else "live",
            }
        )
        for i, q in quotes.items()
    }
    snap = snapshot.model_copy(
        update={
            "at": now,
            "account_at": now,
            "positions": (*snapshot.positions, Position(con_id=10, quantity=100)),
        }
    )
    scenarios = ScenarioEngine(m).build(snap, quiet, now)
    gate = RiskGate(m)
    held = gate.validate(snap, quiet, scenarios, snap.quantities(), now)
    assert not any(r.startswith(("Stale/future quote", "Non-live quote")) for r in held.reasons), held.reasons
    traded = gate.validate(snap, quiet, scenarios, {**snap.quantities(), 10: 150}, now)
    assert "Stale/future quote: 10" in traded.reasons


def test_shares_trade_in_the_pre_market_session_and_options_wait_for_the_open(manifest):
    m, day = contest(manifest), manifest.scoring_sessions[1]
    assert m.pre_market(at(day, 4)) == day and m.pre_market(at(day, 3, 59)) is None
    assert m.extended_session(at(day, 7)) == "pre" and m.extended_session(at(day, 17)) == "post"
    assert m.extended_session(at(day, 11)) is None
    assert m.can_trade(at(day, 4), "STK") and m.can_trade(at(day, 9, 29, 59), "STK")
    assert not m.can_trade(at(day, 3, 59), "STK") and not m.can_trade(at(day, 7), "OPT")
    assert not m.can_trade(at(day, 7), "STK", setup=True)
    assert m.can_trade(at(day, 9, 30), "OPT")  # the regular session
    assert not manifest.can_trade(at(day, 7), "STK") and manifest.extended_session(at(day, 7)) is None


def test_the_overnight_gap_is_spread_over_post_market_overnight_and_pre_market(manifest):
    from hedge.scenarios import PRE_MARKET_VARIANCE

    day, following = manifest.scoring_sessions[:2]
    rest = 0.30 - POST_MARKET_VARIANCE - PRE_MARKET_VARIANCE
    assert trading_variance(at(following, 6, 45), at(following, 9, 30)) == pytest.approx(
        PRE_MARKET_VARIANCE * 2.75 / 5.5
    )
    assert trading_variance(at(day, 20), at(following, 4)) == pytest.approx(rest)
    assert trading_variance(at(day, 23, 30), at(following, 4)) == pytest.approx(rest * 4.5 / 8)
    # From a pre-market start the forecast to the scored close covers what is left of the day.
    assert trading_variance(at(following, 7), at(following, 20)) == pytest.approx(
        PRE_MARKET_VARIANCE * 2.5 / 5.5 + 1 + POST_MARKET_VARIANCE
    )


def test_pre_market_options_are_valued_from_the_live_underlying(tmp_path):
    m = contest(demo_manifest())
    now = at(m.scoring_sessions[1], 7, 15)
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(feed(m, now)))
    quotes = MoomooData(m, path).quotes(now)
    stock = next(c for c in m.contracts if c.kind == "STK")
    option = next(c for c in m.contracts if c.kind == "OPT")
    assert quotes[stock.con_id].data_type == "live" and quotes[option.con_id].data_type == "model"


def test_pre_market_share_orders_are_limits_flagged_outside_regular_hours(manifest, monkeypatch):
    import hedge.broker as broker_module
    from hedge.broker import IBKRBroker
    from hedge.models import OrderRequest

    m = contest(manifest)
    day = m.scoring_sessions[1]
    broker = IBKRBroker(m, asyncio.Queue())
    placed = []
    broker.ib = SimpleNamespace(
        isConnected=lambda: True,
        placeOrder=lambda contract, order: (
            placed.append(order) or SimpleNamespace(order=SimpleNamespace(orderId=1, permId=2))
        ),
    )
    broker.readonly, broker.qualified, broker.reconciled = False, True, True
    broker.native = {10: object()}
    monkeypatch.setattr(broker_module, "utcnow", lambda: at(day, 7))
    request = OrderRequest(
        intent_id="x",
        con_id=10,
        signed_quantity=100,
        limit_price=Decimal("195.00"),
        account=m.account,
        snapshot_id="s",
        decision_id="d",
    )
    asyncio.run(broker.submit(request, 1))
    assert placed[-1].outsideRth is True


def test_the_nav_stream_starts_a_new_base_at_the_open(manifest, snapshot):
    from hedge.valuation import NAVTracker

    m = contest(manifest)
    day = m.scoring_sessions[1]
    tracker = NAVTracker(m)
    pre = at(day, 9, 29)
    base = snapshot.model_copy(
        update={"at": pre, "account_at": pre, "pnl_daily": Decimal("0"), "pnl_at": pre}
    )
    assert tracker.estimate(base, {}, m.contract_map) == pytest.approx(1_000_000)
    assert tracker.pnl_phase == "pre"
    opened = at(day, 9, 30, 2)
    regular = base.model_copy(update={"at": opened, "pnl_daily": Decimal("40"), "pnl_at": opened})
    tracker.estimate(regular, {}, m.contract_map)
    assert tracker.source == "fill attribution" and tracker.pnl_anchor is None  # waits for a settled push


def test_same_index_etfs_keep_their_correlation_while_single_names_are_shrunk(manifest, tmp_path):
    import numpy as np
    import pandas as pd

    from hedge.scenarios import ScenarioEngine

    now = at(manifest.scoring_sessions[0], 17)
    days = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=80)
    rng = np.random.default_rng(3)
    market = rng.normal(0, 0.01, len(days))
    rows = []
    for symbol, beta, noise in [("SPY", 1.0, 0.0002), ("VOO", 1.0, 0.0002), ("BA", 1.3, 0.004)]:
        prices = 100 * np.exp(np.cumsum(beta * market + rng.normal(0, noise, len(days))))
        rows += [
            {"date": str(d.date()), "symbol": symbol, "close": p} for d, p in zip(days, prices, strict=True)
        ]
    path = tmp_path / "history.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    m = manifest.model_copy(update={"daily_stock_history": str(path)})
    cov = ScenarioEngine(m).daily_covariance(now, ["BA", "SPY", "VOO"])
    corr = cov / np.sqrt(np.outer(np.diag(cov), np.diag(cov)))
    assert corr[1, 2] > 0.99  # SPY-VOO: the empirical ~0.999 is kept
    assert corr[0, 1] < 0.85  # BA-SPY: shrunk toward independence as before


def test_a_crossed_after_hours_stock_book_values_but_never_trades(tmp_path, manifest, snapshot, quotes):
    m = contest(demo_manifest())
    now = at(m.scoring_sessions[0], 19, 35)
    raw = feed(m, now)
    stock = next(c for c in m.contracts if c.kind == "STK")
    option = next(c for c in m.contracts if c.kind == "OPT")
    raw["securities"][code_for(stock)]["order_book"] = {"Bid": [[195.06, 300]], "Ask": [[195.00, 200]]}
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(raw))
    feed_reader = MoomooData(m, path)
    got = feed_reader.quotes(now)
    q = got[stock.con_id]
    assert q.data_type == "model" and q.bid == q.ask == pytest.approx(195.03) and q.bid_size == 0
    assert got[option.con_id].data_type == "model" and got[option.con_id].spot == pytest.approx(195.03)
    assert any("Crossed post-market book" in e for e in feed_reader.errors)
    # The same crossed book during regular hours is still rejected.
    regular = at(m.scoring_sessions[0], 11)
    raw = feed(m, regular)
    raw["securities"][code_for(stock)]["order_book"] = {"Bid": [[195.06, 300]], "Ask": [[195.00, 200]]}
    path.write_text(json.dumps(raw))
    assert stock.con_id not in MoomooData(m, path).quotes(regular)


def test_the_risk_gate_holds_a_crossed_stock_but_refuses_to_trade_it(manifest, snapshot, quotes):
    from hedge.risk import RiskGate
    from hedge.scenarios import ScenarioEngine

    m = contest(manifest)
    now = at(m.scoring_sessions[0], 19, 35)
    valued = {
        i: q.model_copy(update={"observed_at": now, "greek_at": now, "data_type": "model"})
        for i, q in quotes.items()
    }
    snap = snapshot.model_copy(
        update={
            "at": now,
            "account_at": now,
            "positions": (*snapshot.positions, Position(con_id=10, quantity=100)),
        }
    )
    scenarios = ScenarioEngine(m).build(snap, valued, now)
    gate = RiskGate(m)
    held = gate.validate(snap, valued, scenarios, snap.quantities(), now)
    assert not any(r.startswith("Non-live quote") for r in held.reasons), held.reasons
    traded = gate.validate(snap, valued, scenarios, {**snap.quantities(), 10: 50}, now)
    assert "Non-live quote: 10" in traded.reasons

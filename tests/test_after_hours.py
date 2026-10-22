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
from hedge.moomoo_data import MoomooData, code_for
from hedge.scenarios import POST_MARKET_VARIANCE, trading_variance


def contest(manifest):
    return manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})


def at(day, hour, minute=0, second=0):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute, second=second)


def test_shares_trade_through_the_post_market_session_and_options_do_not(manifest):
    m, day = contest(manifest), manifest.scoring_sessions[0]
    assert m.can_trade(at(day, 17), "STK") and not m.can_trade(at(day, 17), "OPT")
    assert m.can_trade(at(day, 19, 57, 59), "STK") and not m.can_trade(at(day, 19, 58), "STK")
    assert not m.can_trade(at(day, 15, 59), "STK")  # the regular close's own cutoff still applies
    assert not m.can_trade(at(day, 20, 30), "STK")
    assert not m.can_trade(at(day, 17), "STK", setup=True)
    assert m.post_market(at(day, 16)) == day and m.post_market(at(day, 20)) is None
    assert not manifest.can_trade(at(day, 17), "STK") and manifest.post_market(at(day, 17)) is None


def test_the_forecast_runs_to_the_20_00_scored_close(manifest):
    m, day = contest(manifest), manifest.scoring_sessions[0]
    assert m.scoring_close(day) == at(day, 20)
    assert m.next_close(at(day, 10)) == at(day, 20) and m.next_close(at(day, 17)) == at(day, 20)
    assert m.next_close(at(day, 20, 1)) == at(manifest.scoring_sessions[1], 20)
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
    stale["securities"][code_for(stock)]["book_push_at"] = (now - timedelta(seconds=30)).isoformat()
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


async def test_paper_trading_starts_after_hours_and_plans_shares_only(manifest, store, quotes):
    from hedge.config import Manifest
    from hedge.scenarios import ScenarioEngine
    from hedge.service import TradingService

    m = contest(Manifest.model_validate({**manifest.model_dump(), "autostart": "paper"}))
    now = at(m.scoring_sessions[0], 17)
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

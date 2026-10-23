"""The contest's scored close is the IBKR Activity Statement NAV (instructor, Oct 23): cash and
accrued interest after the post-market session plus positions at the closing marks."""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from hedge.config import NY
from hedge.models import Contract, Execution
from hedge.statement import (
    StatementTracker,
    marks_fixed_at,
    statement_marks,
    statement_samples,
    statement_value,
)

# The Oct 22 2026 statement (Open Positions: Close Price, Mult 100 for options).
STOCKS = {1: ("RKLB", 1120, "51.22"), 2: ("SPCX", 119, "144.75")}
OPTIONS = {  # con_id: (symbol, quantity, statement close)
    11: ("RKLB", -25, 1.93),
    12: ("RKLB", 25, 6.55),
    13: ("SPCX", 25, 18.4774),
    14: ("SPCX", -50, 9.0497),
    15: ("SPCX", 1, 8.80),
    16: ("SPCX", 25, 3.70),
    17: ("SPY", 25, 10.74),
    18: ("SPY", 25, 14.415),
    19: ("SPY", -17, 12.115),
    20: ("SPY", -20, 15.24),
}


def at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute)


def contest_manifest(manifest):
    contracts = [Contract(con_id=i, symbol=symbol, kind="STK") for i, (symbol, _, _) in STOCKS.items()] + [
        Contract(
            con_id=i,
            symbol=symbol,
            kind="OPT",
            expiry=manifest.scoring_sessions[-1] + timedelta(days=20),
            strike=100,
            right="C",
            multiplier=100,
            underlying_id=next(k for k, v in STOCKS.items() if v[0] == symbol) if symbol != "SPY" else 1,
        )
        for i, (symbol, _, _) in OPTIONS.items()
    ]
    return SimpleNamespace(
        contracts=contracts, contract_map={c.con_id: c for c in contracts}, policy=manifest.policy
    )


def capture(prices, spread=0.0):
    return {
        "quotes": {
            str(i): {"bid": p - spread, "ask": p + spread, "mid": p, "data_type": "live"}
            for i, p in prices.items()
        }
    }


def test_the_oct_22_statement_reconciles_to_the_cent(manifest):
    m = contest_manifest(manifest)
    late = {i: p for i, (s, _, p) in OPTIONS.items() if s == "SPY"}
    early = {i: p for i, (s, _, p) in OPTIONS.items() if s != "SPY"}
    captures = {
        "pre_close": capture(early | {i: 0.01 for i in late}),  # SPY at 16:00 is not its close
        "options_close": capture(late),
    }
    closes = {i: Decimal(p) for i, (_, _, p) in STOCKS.items()}
    marks, missing = statement_marks(m, None, captures, closes)
    assert not missing
    assert all(marks[i]["source"] == "options_close midpoint" for i in late)
    assert all(marks[i]["source"] == "pre_close midpoint" for i in early)
    quantities = {i: q for i, (_, q, _) in STOCKS.items()} | {i: q for i, (_, q, _) in OPTIONS.items()}
    value = statement_value(Decimal("872744.50"), Decimal("1601.06"), quantities, marks, m.contract_map)
    assert value == pytest.approx(983_374.21, abs=0.005)


def test_option_marks_carry_their_closing_spread_as_uncertainty(manifest):
    m = contest_manifest(manifest)
    captures = {"pre_close": capture({12: 6.975, 11: 1.93}, spread=0.425)}
    marks, missing = statement_marks(m, None, captures, {})
    assert marks[12]["source"] == "pre_close midpoint"
    assert (marks[12]["mark"], marks[12]["low"], marks[12]["high"]) == pytest.approx((6.975, 6.55, 7.4))
    assert 1 in missing and 2 in missing  # no regular-session close yet
    held = {12: 25, 11: -25}
    mid = statement_value(0, 0, held, marks, m.contract_map)
    low = statement_value(0, 0, held, marks, m.contract_map, "low")
    high = statement_value(0, 0, held, marks, m.contract_map, "high")
    # Oct 22: RKLB 55P long was marked at its bid, 6.55: the low bound covers it.
    assert low == pytest.approx(mid - 2 * 25 * 100 * 0.425) and high == pytest.approx(
        mid + 2 * 25 * 100 * 0.425
    )
    samples = statement_samples(mid, held, marks, m.contract_map)
    assert len(samples) == 64 and low <= samples.min() and samples.max() <= high
    with pytest.raises(KeyError):
        statement_value(0, 0, {1: 100}, marks, m.contract_map)  # a held share without a close


def test_after_the_close_only_our_trades_move_the_predicted_statement_nav(manifest, snapshot):
    m = contest_manifest(manifest)
    marks = {1: {"mark": 51.22, "low": 51.22, "high": 51.22, "source": "regular-session close"}}
    pushed = SimpleNamespace(
        cash=Decimal("100000"),
        accrued_cash=Decimal("10"),
        account_at=snapshot.at,
        quantities=lambda: {1: 1000},
    )
    tracker = StatementTracker(m)
    day = manifest.scoring_sessions[0]
    assert tracker.estimate(day, pushed, marks, [], {}) == pytest.approx(100_010 + 51_220)
    # A post-market round trip: buy 100 at 51.40, sell 100 at 51.30 (+ $1 commissions each).
    fills = [
        Execution(
            exec_id="b",
            order_id=1,
            account="A",
            con_id=1,
            signed_quantity=100,
            price=Decimal("51.40"),
            at=snapshot.at + timedelta(minutes=5),
        ),
        Execution(
            exec_id="s",
            order_id=2,
            account="A",
            con_id=1,
            signed_quantity=-100,
            price=Decimal("51.30"),
            at=snapshot.at + timedelta(minutes=6),
        ),
    ]
    rolled = tracker.estimate(day, pushed, marks, fills, {"b": 1.0, "s": 1.0})
    assert rolled == pytest.approx(100_010 + 51_220 - 10 - 2)  # exactly the spread paid plus fees
    # A cash push right after the fills is not trusted; a settled one re-bases and is checked.
    settled = SimpleNamespace(
        cash=Decimal("99988"),
        accrued_cash=Decimal("10"),
        account_at=snapshot.at + timedelta(minutes=10),
        quantities=lambda: {1: 1000},
    )
    assert tracker.estimate(day, settled, marks, fills, {"b": 1.0, "s": 1.0}) == pytest.approx(rolled)
    assert tracker.last_check["settled"] == pytest.approx(tracker.last_check["rolled"])


def test_the_service_shows_the_predicted_statement_nav_after_the_closing_marks(manifest, store):
    from hedge.models import digest
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    clock = [at(day, 15)]
    service.now = lambda: clock[0]
    assert service.statement_estimate() is None  # marks not fixed before 16:16
    clock[0] = marks_fixed_at(manifest, day)
    service.broker.positions = {10: 200}
    service.broker.now = clock[0]
    service.last_snapshot = service.broker.snapshot()
    stock = manifest.contract_map[10]
    assert stock.kind == "STK"
    waiting = service.statement_estimate()
    assert waiting["status"] == "waiting" and "No closing mark" in waiting["reason"]
    service.official_closes[day] = {10: Decimal("194.50")}
    for phase in ("pre_close", "options_close"):
        store.record(
            "close_marks",
            manifest.account,
            {"quotes": {}},
            digest((manifest.contest_id, str(day), phase))[:32],
        )
    estimate = service.statement_estimate()
    snap = service.broker.snapshot()
    expected = float(snap.cash) + float(snap.accrued_cash or 0) + 200 * 194.50
    assert estimate["status"] == "ok" and estimate["value"] == pytest.approx(expected)
    assert estimate["low"] == estimate["high"] == pytest.approx(expected)
    assert np.allclose(estimate["samples"], expected)
    state = service.state()
    assert state["nav_feed"] == "predicted statement NAV (closing marks)"
    assert state["nav_estimate"] == pytest.approx(expected) and state["statement_estimate"]["status"] == "ok"
    assert "samples" not in state["statement_estimate"]


def test_spy_options_stay_live_until_their_16_15_close(manifest, tmp_path):
    from hedge.moomoo_data import MoomooData

    m = manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})
    data = MoomooData(m, tmp_path / "latest.json")
    day = m.scoring_sessions[0]
    spy, rklb = SimpleNamespace(kind="OPT", symbol="SPY"), SimpleNamespace(kind="OPT", symbol="RKLB")
    assert data._in_session(spy, at(day, 16, 10)) and not data._in_session(spy, at(day, 16, 15))
    assert not data._in_session(rklb, at(day, 16, 1))


def test_the_close_marks_include_spy_options_16_15_close(manifest, store):
    from hedge.closing import CloseMarkRecorder

    contracts = tuple(
        c.model_copy(update={"symbol": "SPY"}) if c.kind == "OPT" else c for c in manifest.contracts
    )
    m = manifest.model_copy(update={"contracts": contracts})
    day = m.scoring_sessions[0]
    clock = [at(day, 16, 13) + timedelta(seconds=30)]
    recorder = CloseMarkRecorder(
        m, store, lambda: clock[0], lambda now, d, phase: {"at": now.isoformat(), "phase": phase}, print
    )
    recorder.tick()
    clock[0] = at(day, 16, 14) + timedelta(seconds=50)
    recorder.tick()
    clock[0] = at(day, 16, 15) + timedelta(seconds=1)
    recorder.tick()
    rows = [r for r in store.records("close_marks", m.account) if r.get("phase") == "options_close"]
    assert [r["at"] for r in rows] == [(at(day, 16, 14) + timedelta(seconds=50)).isoformat()]

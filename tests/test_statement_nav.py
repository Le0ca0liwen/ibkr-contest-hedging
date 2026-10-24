"""The contest's scored close is the IBKR Activity Statement NAV (instructor, Oct 23): cash and
accrued interest after the post-market session plus positions at the closing marks."""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from hedge.config import NY
from hedge.models import Contract, Execution
from hedge.moomoo_data import code_for
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
    captures = {
        "pre_close": capture({i: p for i, (_, _, p) in OPTIONS.items()}),
        "options_close": capture({i: 99.0 for i in late}),  # Oct 23: SPY's 16:15 book is not its close
    }
    closes = {i: Decimal(p) for i, (_, _, p) in STOCKS.items()}
    marks, missing = statement_marks(m, None, captures, closes)
    assert not missing
    assert all(marks[i]["source"] == "pre_close midpoint" for i in OPTIONS)
    only_late, _ = statement_marks(m, None, {"options_close": capture(late)}, closes)
    assert all(only_late[i]["source"] == "options_close midpoint" for i in late)  # the last resort
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


def test_a_fill_without_a_closing_mark_leaves_the_estimate_waiting_until_a_settled_push(manifest, store):
    """Review (Oct 23): closes not fetched and no shares held, then a burner round trip in shares
    after the base: the estimate waits (no KeyError through state()) and a settled push heals it."""
    from hedge.models import digest
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    clock = [marks_fixed_at(manifest, day)]
    service.now = lambda: clock[0]
    service.broker.positions = {101: 25, 102: 25}  # options only
    service.broker.now = clock[0]
    service.last_snapshot = service.broker.snapshot()
    quotes = {str(i): {"bid": 5.0, "ask": 5.2, "data_type": "live"} for i in (101, 102)}
    for phase in ("pre_close", "options_close"):
        store.record(
            "close_marks",
            manifest.account,
            {"quotes": quotes},
            digest((manifest.contest_id, str(day), phase))[:32],
        )
    assert service.statement_estimate()["status"] == "ok"
    base_at = service.statement.base_at
    for exec_id, signed, price, seconds in (("b", 100, "194.6", 60), ("s", -100, "194.5", 65)):
        service.nav_tracker.execution(
            Execution(
                exec_id=exec_id,
                order_id=1,
                account=manifest.account,
                con_id=10,
                signed_quantity=signed,
                price=Decimal(price),
                at=base_at + timedelta(seconds=seconds),
            )
        )
    clock[0] += timedelta(minutes=2)
    waiting = service.statement_estimate()
    assert waiting["status"] == "waiting" and "No closing mark yet for" in waiting["reason"]
    assert service.state()["statement_estimate"]["status"] == "waiting"
    service.broker.now = clock[0]  # a push 55 s after the last fill: settled, and the book is flat
    service.last_snapshot = service.broker.snapshot()
    healed = service.statement_estimate()
    assert healed["status"] == "ok" and service.statement.last_check["rolled"] is None


async def test_one_refused_share_keeps_the_other_regular_session_closes(manifest):
    from datetime import date

    from ib_async import RequestError

    from hedge.broker import IBKRBroker

    day = date(2026, 10, 23)

    class History:
        def __init__(self, refused):
            self.refused = refused

        async def reqHistoricalDataAsync(self, native, **kw):
            if native.symbol in self.refused:
                raise RequestError(5, 162, "No market data permissions for ARCA STK")
            return [
                SimpleNamespace(date=date(2026, 10, 22), close=50.0),
                SimpleNamespace(date=day, close=51.22),
            ]

    broker = IBKRBroker.__new__(IBKRBroker)
    broker.native = {
        i: SimpleNamespace(secType="STK", symbol=s) for i, s in ((1, "RKLB"), (2, "VOO"), (3, "SPCX"))
    }
    broker.ib = History({"VOO"})
    assert await broker.official_closes(day, [1, 2, 3]) == {1: Decimal("51.22"), 3: Decimal("51.22")}
    broker.ib = History({"RKLB", "VOO", "SPCX"})
    with pytest.raises(RequestError):  # nothing at all: the service alerts and retries
        await broker.official_closes(day, [1, 2, 3])


async def test_missed_closing_captures_fall_back_to_moomoos_archive_and_final_books(
    manifest, store, tmp_path
):
    """Oct 23: the Mac slept from 15:56, so no 16:00 capture. The collector's archive still had each
    option's book at 15:56 and Moomoo kept its final book after the close."""
    import gzip
    import json

    from hedge.moomoo_data import code_for
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    now = marks_fixed_at(manifest, day) + timedelta(minutes=4)
    service.now = lambda: now
    service.broker.positions = {10: 200, 101: -5, 102: 3}
    service.broker.now = now
    service.last_snapshot = service.broker.snapshot()
    service.moomoo_data = SimpleNamespace(path=tmp_path / "latest.json")
    first, second = (code_for(manifest.contract_map[i]) for i in (101, 102))

    def book(bid, ask, at):
        stamp = f"{at:%Y-%m-%d %H:%M:%S}.000"
        return {
            "order_book": {
                "Bid": [[bid, 5, 1, []]],
                "Ask": [[ask, 5, 1, []]],
                "svr_recv_time_bid": stamp,
                "svr_recv_time_ask": stamp,
            },
            "book_push_count": 3,
        }

    early = at(day, 15, 56)
    with gzip.open(tmp_path / f"raw-{day:%Y%m%d}.jsonl.gz", "wt") as archive:
        archive.write(
            json.dumps({"received_at": early.isoformat(), "securities": {first: book(6.0, 6.2, early)}})
            + "\n"
        )
    late = at(day, 16, 30)
    pushed = {"book_push_at": late.isoformat()}
    (tmp_path / "latest.json").write_text(
        json.dumps(
            {
                "securities": {
                    first: {**book(5.0, 7.0, late), **pushed},
                    second: {**book(2.0, 2.2, late), **pushed},
                }
            }
        )
    )
    await service._fallback_close_marks(day, now)
    await service._fallback_close_marks(day, now)  # once per 5 minutes; records are insert-once
    phases = {r["phase"] for r in store.records("close_marks", manifest.account)}
    assert phases == {"archive_close", "final_book"}
    service.official_closes[day] = {10: Decimal("194.50")}
    estimate = service.statement_estimate()
    assert estimate["status"] == "ok"
    sources = {label: m["source"] for label, m in estimate["marks"].items()}
    assert sources[manifest.contract_map[101].label] == "archive_close midpoint"  # 15:56 beats the final book
    assert sources[manifest.contract_map[102].label] == "final_book midpoint"
    alerts = [a["message"] for a in store.records("alert", manifest.account)]
    assert any("were not captured live" in a for a in alerts)


def test_pasted_open_positions_are_compared_with_the_predicted_marks(manifest, store):
    from fastapi.testclient import TestClient

    from hedge.models import digest
    from hedge.service import TradingService
    from hedge.webapp import create_app

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    option = manifest.contract_map[101]
    store.record(
        "close_marks",
        manifest.account,
        {"quotes": {"101": {"bid": 6.55, "ask": 7.40, "data_type": "live"}}},
        digest((manifest.contest_id, str(day), "pre_close"))[:32],
    )
    service.official_closes[day] = {10: Decimal("194.50")}
    stock = manifest.contract_map[10]
    text = "\n".join(
        [
            "Symbol\tQuantity\tMult\tCost Price\tCost Basis\tClose Price\tValue\tUnrealized P/L\tCode",
            f"{stock.symbol}\t100\t1\t190\t19,000.00\t194.5000\t19,450.00\t450.00\t",
            f"{option.symbol} {option.expiry:%d%b%y}".upper()
            + f" {option.strike:g} {option.right}\t25\t100\t5\t12,500.00\t6.5500\t16,375.00\t3,875.00\t",
        ]
    )
    client = TestClient(create_app(service))
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    response = client.post(
        "/api/statement_positions", json={"date": str(day), "text": text}, headers={"X-Control-Token": token}
    )
    assert response.status_code == 200, response.text
    rows = {r["label"]: r for r in response.json()["comparison"]["rows"]}
    assert rows[option.label]["predicted"] == pytest.approx(6.975) and rows[option.label]["statement"] == 6.55
    assert rows[option.label]["effect"] == pytest.approx(25 * 100 * (6.55 - 6.975))  # marked at the bid
    assert rows[stock.symbol]["effect"] == pytest.approx(0)
    assert service.state()["statement_marks_check"]["total_effect"] == pytest.approx(-1062.5)
    # Every estimator is scored: here the long put at its worst side (the bid) was exactly right.
    estimators = response.json()["comparison"]["estimators"]
    assert estimators["mid"] == {
        "effect": pytest.approx(-1062.5),
        "abs_effect": pytest.approx(1062.5),
        "contracts": 1,
    }
    assert estimators["bid_ask_worst"]["abs_effect"] == pytest.approx(0)
    assert rows[option.label]["estimates"] == {
        "mid": pytest.approx(6.975),
        "bid_ask_worst": 6.55,
        "ibkr_1956": None,
        "ibkr_last": None,
    }
    bad = client.post(
        "/api/statement_positions",
        json={"date": str(day), "text": "hello"},
        headers={"X-Control-Token": token},
    )
    assert bad.status_code == 422


def test_a_chain_before_the_close_prices_options_on_the_smoothed_curve():
    """Oct 23 at 15:56: SPCX 130C 19.60/20.40 sat above the smile (IV 55.9% vs 53.3/52.7%); the
    clearing house's smoothing would mark it below its 20.00 midpoint, inside its bid/ask."""
    from datetime import date

    from hedge.moomoo_data import code_for

    expiry = date(2026, 11, 20)
    legs = {
        13: (130, "C", 19.60, 20.40),
        14: (145, "C", 9.85, 10.00),
        16: (160, "C", 4.00, 4.10),
        15: (145, "P", 7.30, 7.45),
    }
    contracts = [
        Contract(con_id=i, symbol="SPCX", kind="OPT", expiry=expiry, strike=k, right=r, multiplier=100)
        for i, (k, r, _, _) in legs.items()
    ]
    m = SimpleNamespace(
        contracts=contracts, contract_map={c.con_id: c for c in contracts}, risk_free_rate=0.0344
    )
    quotes = {str(i): {"bid": b, "ask": a, "data_type": "live"} for i, (_, _, b, a) in legs.items()}
    chain = {
        "at": "2026-10-23T19:59:00+00:00",
        "underlyings": {"SPCX": {"spot": 147.17}},
        "options": [
            {
                "code": code_for(c),
                "underlying": "SPCX",
                "expiry": str(expiry),
                "strike": c.strike,
                "right": c.right,
                "bid": legs[c.con_id][2],
                "ask": legs[c.con_id][3],
            }
            for c in contracts
        ],
    }
    plain, _ = statement_marks(m, None, {"pre_close": {"quotes": quotes}}, {})
    marks, missing = statement_marks(m, None, {"pre_close": {"quotes": quotes}}, {}, {"pre_close": chain})
    assert (
        not missing
        and plain[13]["mark"] == pytest.approx(20.0)
        and plain[13]["source"] == "pre_close midpoint"
    )
    assert 19.6 <= marks[13]["mark"] < 19.97 and marks[13]["source"].startswith("pre_close smoothed IV")
    assert marks[13]["mid"] == pytest.approx(20.0) and (marks[13]["low"], marks[13]["high"]) == (19.6, 20.4)
    assert marks[14]["mark"] == pytest.approx(9.925, abs=0.05)  # a tight quote on the curve barely moves
    # The 16:00 book moved after the chain (SPCX up): the smoothing offset follows the new book.
    moved = {k: {**q, "bid": q["bid"] + 0.3, "ask": q["ask"] + 0.3} for k, q in quotes.items() if k == "13"}
    later, _ = statement_marks(
        m, None, {"pre_close": {"quotes": {**quotes, **moved}}}, {}, {"pre_close": chain}
    )
    assert later[13]["mark"] == pytest.approx(marks[13]["mark"] + 0.3)


async def test_chains_are_listed_at_1555_and_quoted_at_each_option_close(
    manifest, store, tmp_path, monkeypatch
):
    """Codes at 15:55 (slow listing), quotes at 15:59:57 for every held chain (OCC's 4:00 p.m. NBBO)
    and again at 16:14:57 for SPY-style options; each chain gets its OCC rebuild once."""
    from hedge import config
    from hedge import service as service_module
    from hedge.service import TradingService

    monkeypatch.setattr(config, "LATE_OPTION_SYMBOLS", frozenset({"BA"}))
    monkeypatch.setattr(service_module, "LATE_OPTION_SYMBOLS", frozenset({"BA"}))
    service = TradingService(manifest, store, demo=True)
    service.moomoo_data = SimpleNamespace(path=tmp_path / "latest.json")
    service.broker.positions = {101: -5}
    service.last_snapshot = service.broker.snapshot()
    calls, rebuilt = [], []

    async def fetch(symbols, expiries, path, list_only=False, codes=None, at=None):
        calls.append((path.name, list_only, codes.name if codes else None, at))
        if list_only:
            path.write_text("{}")
            return None
        return {"at": "2026-10-07T19:59:58+00:00", "underlyings": {}, "options": [{"code": "x"}]}

    async def occ(chain):
        rebuilt.append(chain["phase"])
        return {"US.BA": {"mark": 1.0, "low": 0.9, "high": 1.1, "source": "OCC rebuild (9 strikes)"}}

    service._fetch_chain, service._rebuild_occ = fetch, occ
    day = manifest.scoring_sessions[0]
    stamp = lambda h, m, s=0: at(day, h, m) + timedelta(seconds=s)  # noqa: E731
    stale = tmp_path / f"chain-{day:%Y%m%d}-listing.json"
    attempted = set()
    for moment in (stamp(15, 54), stamp(15, 55, 2), stamp(15, 56), stamp(15, 59, 20)):
        await service._chain_step(moment, attempted)
    assert [c[:3] for c in calls] == [(stale.name, True, None)]
    await service._chain_step(stamp(15, 59, 30), attempted)
    await service._chain_step(stamp(15, 59, 35), attempted)  # once per day
    assert calls[1] == (f"chain-{day:%Y%m%d}-pre_close.json", False, stale.name, stamp(15, 59, 57))
    await service._chain_step(stamp(16, 14, 28), attempted)
    assert calls[2] == (f"chain-{day:%Y%m%d}-options_close.json", False, stale.name, stamp(16, 14, 57))
    assert len(calls) == 3 and rebuilt == ["pre_close", "options_close"]
    await service._chain_step(stamp(16, 20), attempted)
    assert rebuilt == ["pre_close", "options_close"]  # stored once, never recomputed
    stored = store.records("occ_marks", manifest.account)
    assert {r["phase"] for r in stored} == {"pre_close", "options_close"}
    assert all(r["marks"]["US.BA"]["mark"] == 1.0 and r["params"] for r in stored)
    _, chains = service._close_inputs(day)
    assert chains["pre_close"]["occ"]["US.BA"]["source"].startswith("OCC rebuild")


async def test_a_failed_occ_rebuild_alerts_once_and_keeps_the_chain(manifest, store, tmp_path, monkeypatch):
    from hedge.models import digest
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    store.record(
        "option_chain",
        manifest.account,
        {"contest": manifest.contest_id, "date": str(day), "phase": "pre_close", "options": []},
        digest((manifest.contest_id, str(day), "pre_close_chain"))[:32],
    )
    tries = []

    async def broken(chain):
        tries.append(1)
        raise RuntimeError("solver")

    service._rebuild_occ = broken
    await service._ensure_occ(day)
    await service._ensure_occ(day)
    assert tries == [1] and not store.records("occ_marks", manifest.account)
    assert any(
        "OCC closing-price rebuild failed" in a["message"] for a in store.records("alert", manifest.account)
    )
    _, chains = service._close_inputs(day)
    assert chains["pre_close"]["occ"] == {}


def test_ibkr_evening_marks_are_logged_every_three_minutes_and_split_at_1956(manifest, store):
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    prices = iter(range(1, 1000))
    service.broker.portfolio_marks = lambda: {
        101: {"market_price": float(next(prices))},
        999: {"market_price": 5},
    }
    day = manifest.scoring_sessions[0]
    service._evening_marks_step(at(day, 15, 58))  # before the close: nothing
    for minutes in range(0, 9 * 60 + 10, 1):
        service._evening_marks_step(at(day, 16) + timedelta(minutes=minutes))
    rows = store.records("evening_marks", manifest.account, 1000)
    assert len(rows) == 9 * 20 + 1 and all(set(r["marks"]) == {"101"} for r in rows)
    before, last = service._evening_marks(day)[101]
    assert before == 234 // 3 + 1  # the 19:54 row: the last one before the burner stops at 19:56
    assert last == len(rows)


def test_statement_marks_prefer_the_stored_occ_rebuild_and_keep_every_estimate():
    contract = Contract(
        con_id=13, symbol="SPCX", kind="OPT", expiry=datetime(2026, 11, 20).date(), strike=130, right="C"
    )
    m = SimpleNamespace(contracts=[contract], contract_map={13: contract}, risk_free_rate=0.0344)
    quotes = {"13": {"bid": 19.6, "ask": 20.4, "data_type": "live"}}
    code = code_for(contract)
    chain = {
        "at": "2026-10-23T19:59:57+00:00",
        "underlyings": {},
        "options": [],
        "occ": {code: {"mark": 19.7, "low": 19.5, "high": 20.3, "source": "OCC rebuild (12 strikes)"}},
    }
    marks, missing = statement_marks(m, None, {"pre_close": {"quotes": quotes}}, {}, {"pre_close": chain})
    mark = marks[13]
    assert not missing and mark["source"] == "pre_close OCC rebuild (12 strikes)"
    # The chain's book was 0.2 below the 16:00 book: only the rebuild's offset (-0.2) carries over.
    assert mark["mark"] == pytest.approx(19.8)
    assert mark["estimates"] == {"mid": pytest.approx(20.0), "occ": pytest.approx(19.8)}


def test_the_dashboard_shows_the_statement_style_value_before_the_close(manifest, store, quotes, now):
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    service.broker.update_quotes(quotes, now)
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), quotes
    snap = service.last_snapshot
    expected = (
        float(snap.cash)
        + float(snap.accrued_cash or 0)
        + sum(q * manifest.contract_map[i].multiplier * quotes[i].mid for i, q in snap.quantities().items())
    )
    assert service.state()["statement_basis_nav"] == pytest.approx(expected)
    service.now = lambda: marks_fixed_at(manifest, manifest.active_session(now))
    assert service.statement_basis_now() is None  # from 16:16 the predicted statement NAV is shown


def test_samples_centre_on_the_mark_and_stay_inside_the_book():
    """Review (Oct 24): with a smoothed mark off the midpoint the burner's samples must centre on
    the mark, not on the midpoint, and never leave the closing bid/ask."""
    contracts = {12: SimpleNamespace(multiplier=100)}
    centred = {12: {"mark": 6.975, "low": 6.55, "high": 7.40, "mid": 6.975}}
    samples = statement_samples(0.0, {12: 25}, centred, contracts, n=4000)
    assert abs(samples.mean()) < 30
    pinned = {12: {"mark": 6.55, "low": 6.55, "high": 7.40, "mid": 6.975}}  # Oct 22: marked at the bid
    samples = statement_samples(0.0, {12: 25}, pinned, contracts, n=4000)
    assert samples.min() >= 0 and samples.max() <= 25 * 100 * 0.85 + 1e-6
    assert np.median(samples) < 25 * 100 * 0.425  # around the mark, not $1,062 above it


async def test_a_book_from_before_a_sleep_is_never_taken_as_the_closing_book(manifest, store, tmp_path):
    import json

    from hedge.moomoo_data import code_for
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    now = marks_fixed_at(manifest, day) + timedelta(minutes=30)
    service.broker.positions = {101: -5}
    service.last_snapshot = service.broker.snapshot()
    service.moomoo_data = SimpleNamespace(path=tmp_path / "latest.json")
    code = code_for(manifest.contract_map[101])
    book = {"order_book": {"Bid": [[6.0, 5, 1, []]], "Ask": [[6.2, 5, 1, []]]}, "book_push_count": 3}
    (tmp_path / "latest.json").write_text(
        json.dumps({"securities": {code: {**book, "book_push_at": at(day, 15, 30).isoformat()}}})
    )
    await service._fallback_close_marks(day, now)
    assert not store.records("close_marks", manifest.account)  # the 15:30 book is not a close
    (tmp_path / "latest.json").write_text(json.dumps({"securities": {code: book}}))  # no time at all
    service.fallback_marks_at.clear()
    await service._fallback_close_marks(day, now)
    assert not store.records("close_marks", manifest.account)
    assert not any("unavailable" in a["message"] for a in store.records("alert", manifest.account))

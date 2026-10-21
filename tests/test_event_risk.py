import asyncio
import json
import re
import threading
import time
import warnings
from datetime import date, datetime
from decimal import Decimal

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hedge.config import NY, Manifest
from hedge.demo import demo_quotes
from hedge.event_risk import (
    EventsBusy,
    EventsLimit,
    calendar_models,
    checked_events,
    grounded_tools,
    propose,
)
from hedge.models import Contract, PortfolioSnapshot, Position, Quote
from hedge.optimizer import HedgeOptimizer
from hedge.risk import RiskGate
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService
from hedge.storage import Store
from hedge.supervisor import ADVISORY
from hedge.valuation import ValuationEngine
from hedge.webapp import SUGGESTIONS_CSP, create_app
from surplus_addon.models import Input

SECRET = "test-credential-4242"
D0, D1, D2 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)
KEYS = ("RKLB", "SPCX", "SPY")
NOV19, NOV20, NOV23, NOV27 = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 27)
SPOTS = {"SPCX": 145.0, "RKLB": 50.0, "SPY": 770.0, "IVV": 773.0}
IVS = {"SPCX": 0.65, "RKLB": 0.70, "SPY": 0.16, "IVV": 0.16}


def et(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


# ------------------------------------------------------------------ contest-shaped universe
def contest_manifest(path, ivv=False):
    """The measured contest universe (docs/CONTEST_RESTRICTIONS.md): the seven mandated legs plus about
    12 tradable candidates (SPY Nov 19/23 770C/P, SPCX Nov 20 145P and Nov 27 145C/P, RKLB Nov 20 50P
    and Nov 27 50C/P, SPCX and RKLB shares); `ivv` adds an IVV index-proxy hedge."""
    stocks = [(10, "SPCX"), (20, "RKLB"), (30, "SPY"), *([(40, "IVV")] if ivv else [])]
    underlying = {"SPCX": 10, "RKLB": 20, "SPY": 30}
    specs = [
        (101, "SPCX", NOV20, 130, "C"),
        (102, "SPCX", NOV20, 145, "C"),
        (103, "SPCX", NOV20, 160, "C"),
        (104, "RKLB", NOV20, 45, "P"),
        (105, "RKLB", NOV20, 55, "P"),
        (106, "SPY", NOV20, 770, "C"),
        (107, "SPY", NOV20, 770, "P"),
        (201, "SPY", NOV19, 770, "C"),
        (202, "SPY", NOV19, 770, "P"),
        (203, "SPY", NOV23, 770, "C"),
        (204, "SPY", NOV23, 770, "P"),
        (205, "SPCX", NOV20, 145, "P"),
        (206, "SPCX", NOV27, 145, "C"),
        (207, "SPCX", NOV27, 145, "P"),
        (208, "RKLB", NOV20, 50, "P"),
        (209, "RKLB", NOV27, 50, "C"),
        (210, "RKLB", NOV27, 50, "P"),
    ]
    contracts = [Contract(con_id=i, symbol=s, kind="STK") for i, s in stocks] + [
        Contract(
            con_id=i,
            symbol=s,
            kind="OPT",
            multiplier=100,
            expiry=e,
            strike=k,
            right=r,
            underlying_id=underlying[s],
        )
        for i, s, e, k, r in specs
    ]
    return Manifest(
        name="Contest-shaped test",
        account="DU_TEST",
        day_zero=D0,
        scoring_sessions=(D1, D2, date(2026, 10, 9)),
        valuation_convention="IBKR reported closing NAV",
        report_source="test",
        contracts=tuple(contracts),
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=NOV20,
        mandatory={101: 25, 102: -50, 103: 25, 104: -25, 105: 25, 106: 25, 107: 25},
        universe="open",
        scoring_rule="calendar_interest_benchmark",
        scoring_annual_rate=Decimal("0.0344"),
        daily_stock_history=str(path),
        confirmed=True,
        # As config/contest.template.yaml: $20 certified band, overnight term on.
        policy={
            "primary_band": 20,
            "cash_rate": 0.0344,
            "cash_rate_status": "modeled",
            "cash_rate_source": "test",
            "overnight_weight": 0.5,
        },
    )


def daily_history(path, now):
    days = pd.bdate_range(end=pd.Timestamp(now.date()) - pd.Timedelta(days=1), periods=70)
    rng = np.random.default_rng(11)
    market = rng.normal(0, 0.01, len(days))
    rows = []
    for symbol, beta, noise in (
        ("SPCX", 1.6, 0.03),
        ("RKLB", 1.8, 0.035),
        ("SPY", 1.0, 0.001),
        ("IVV", 1.0, 0.001),
    ):
        prices = SPOTS[symbol] * np.exp(np.cumsum(beta * market + rng.normal(0, noise, len(days))))
        rows += [
            {"date": str(d.date()), "symbol": symbol, "close": float(p)}
            for d, p in zip(days, prices, strict=True)
        ]
    pd.DataFrame(rows).to_csv(path, index=False)


def contest_quotes(m, now):
    valuation, quotes = ValuationEngine(m), {}
    for c in m.contracts:
        spot, iv = SPOTS[c.symbol], IVS[c.symbol]
        price = valuation.price(c, spot, iv, now)
        spread = max(0.05, round(0.01 * price, 2)) if c.kind == "OPT" else 0.02
        quotes[c.con_id] = Quote(
            provider="synthetic",
            con_id=c.con_id,
            bid=max(0.01, price - spread / 2),
            ask=price + spread / 2,
            bid_size=20 if c.kind == "OPT" else 500,
            ask_size=20 if c.kind == "OPT" else 500,
            observed_at=now,
            data_type="live",
            spot=spot,
            iv=iv if c.kind == "OPT" else None,
            greek_at=now,
            model_price=price,
            shortable_shares=100000 if c.kind == "STK" else None,
            **(valuation.greeks(c, spot, iv, now) if c.kind == "OPT" else {}),
        )
    return quotes


def contest_snapshot(m, quotes, now):
    mark = sum(quotes[i].mid * q * m.contract_map[i].multiplier for i, q in m.mandatory.items())
    return PortfolioSnapshot(
        account=m.account,
        at=now,
        account_at=now,
        nav=Decimal("1000000"),
        cash=Decimal(str(round(1_000_000 - mark, 2))),
        excess_liquidity=Decimal("800000"),
        initial_margin=Decimal("200000"),
        positions=tuple(Position(con_id=i, quantity=q) for i, q in m.mandatory.items()),
    )


@pytest.fixture
def contest_frame(tmp_path):
    now = et(D1, 10)
    daily_history(tmp_path / "daily.csv", now)
    m = contest_manifest(tmp_path / "daily.csv")
    quotes = contest_quotes(m, now)
    return m, quotes, contest_snapshot(m, quotes, now), now


# ------------------------------------------------------------------ services and Google replies
def make_service(tmp_path, monkeypatch, manifest, handler=None, enabled=False, store=None, now=None):
    monkeypatch.chdir(tmp_path)
    if enabled:
        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    if store is None:
        store = Store(f"sqlite:///{tmp_path}/events.db")
        store.initialize()
    service = TradingService(manifest, store, demo=True)

    def forbidden(request):
        raise AssertionError("AI off: Google must not be called")

    service.supervisor.transport = httpx.MockTransport(handler or forbidden)
    if now is not None:
        service.now = lambda: now
    return service


def event(**fields):
    return {
        "symbols": ["RKLB"],
        "type": "earnings",
        "title": "Rocket Lab Q3 earnings",
        "date": str(D1),
        "timing": "after_close",
        "time_et": None,
        "impact": "high",
        "reason": "Quarterly results after the close usually move the stock sharply.",
        "source_urls": ["https://investors.rocketlabusa.com/news/q3"],
        **fields,
    }


def calendar_body(session=D1, events=None, **extra):
    return {
        "session": str(session),
        "events": events if events is not None else [event()],
        "notes": "",
        **extra,
    }


def google(
    body,
    citations=(),
    suggestions="<div class='chips'><a href='https://www.google.com/search?q=x'>x</a></div>",
):
    return {
        "status": "completed",
        "steps": [
            {"type": "google_search_call", "id": "c1", "arguments": {"queries": ["RKLB earnings date"]}},
            {
                "type": "google_search_result",
                "call_id": "c1",
                "result": [{"search_suggestions": suggestions}],
            },
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(body),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": u,
                                "title": "rocketlabusa.com",
                                "start_index": 0,
                                "end_index": 5,
                            }
                            for u in citations
                        ],
                    }
                ],
            },
        ],
        "usage": {"total_tokens": 99},
    }


def token_of(client):
    return re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)


# ------------------------------------------------------------------ calendar request, validation, proposal
@pytest.mark.asyncio
async def test_calendar_request_is_grounded_and_private(tmp_path, monkeypatch, contest_frame):
    m, *_ = contest_frame
    sent = []
    body = calendar_body(
        events=[
            event(),
            event(
                symbols=["MARKET"],
                type="cpi",
                title="September CPI",
                date=str(D2),
                timing="pre_open",
                source_urls=["https://www.bls.gov/cpi/"],
            ),
        ]
    )

    def handler(request):
        sent.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == SECRET
        return httpx.Response(200, json=google(body, ["https://investors.rocketlabusa.com/news/q3"]))

    service = make_service(tmp_path, monkeypatch, m, handler, enabled=True, now=et(D1, 8, 46))
    calendar = await service.event_risk.refresh("operator", et(D1, 8, 46), D1)
    request = sent[0]
    assert request["tools"] == grounded_tools() == [{"type": "google_search"}]
    assert request["store"] is False and request["background"] is False
    assert request["system_instruction"].endswith(ADVISORY)
    assert "$ref" not in json.dumps(request["response_format"]["schema"])
    payload = json.loads(request["input"])
    assert set(payload) == {"session", "window_start", "window_end", "allowed_symbols", "today_et"}
    assert payload["session"] == str(D1) and payload["allowed_symbols"] == [*KEYS, "MARKET"]
    # The session, its after-close and the next pre-open: next session's open + 15 min.
    assert payload["window_start"] == "2026-10-07T08:46-04:00"
    assert payload["window_end"] == "2026-10-08T09:45-04:00"
    for private in ("DU_TEST", "nav", "1000000", "positions", "account"):
        assert private not in request["input"], private
    assert calendar["status"] == "ok" and len(calendar["events"]) == 2
    assert calendar["proposal"]["factors"] == {"RKLB": 1.5, "SPCX": 1.25, "SPY": 1.5}
    assert (
        calendar["search_queries"] == ["RKLB earnings date"]
        and "chips" in calendar["search_suggestions_html"]
    )
    stored = service.store.records("event_calendar", m.account, 5)
    assert [c["id"] for c in stored] == [calendar["id"]]
    proposal_alerts = [a for a in service.store.records("alert", m.account, 10) if "proposes" in a["message"]]
    assert (
        len(proposal_alerts) == 1
        and "nothing applies until you press Approve caution" in proposal_alerts[0]["message"]
    )


def test_calendar_validation():
    CalendarEvent, Reply = calendar_models(KEYS)
    CalendarEvent.model_validate(event())
    CalendarEvent.model_validate(event(impact="low", source_urls=[]))  # low impact needs no source
    CalendarEvent.model_validate(event(timing="intraday", time_et="14:00"))
    for bad in (
        event(extra="x"),
        event(impact="medium", source_urls=[]),
        event(source_urls=["http://www.bls.gov/cpi/"]),
        event(source_urls=["https://user:pw@www.bls.gov/cpi/"]),
        event(source_urls=["https://user@evil.example.com/"]),
        event(source_urls=["javascript:alert(1)"]),
        event(source_urls=["https://localhost/x"]),
        event(source_urls=["https://www.bls.gov/a b"]),
        event(timing="intraday", time_et=None),
        event(timing="intraday", time_et="25:00"),
        event(symbols=["TSLA"]),
        event(type="rumour"),
        event(title="x"),
    ):
        with pytest.raises(ValidationError):
            CalendarEvent.model_validate(bad)
    with pytest.raises(ValidationError):
        Reply.model_validate(calendar_body(extra="x"))
    start, end = et(D1, 8, 46), et(D2, 9, 45)
    reply = Reply.model_validate(calendar_body(D2))
    with pytest.raises(ValueError, match="Invalid Google AI response rejected"):
        checked_events(reply, D1, start, end, (), KEYS)
    reply = Reply.model_validate(
        calendar_body(
            events=[
                event(),
                event(title="  Rocket Lab Q3 earnings "),  # duplicate after cleaning
                event(title="SPCX launch window", symbols=["SPCX"], type="launch", date="2026-10-12"),
                event(title="Old news", date="2026-10-06"),
                event(
                    title="Fed chair remarks",
                    symbols=["MARKET", "SPY"],
                    type="fomc",
                    timing="intraday",
                    time_et="14:00",
                    source_urls=["https://www.federalreserve.gov/x", "https://www.federalreserve.gov/x"],
                ),
            ]
        )
    )
    citations = ({"url": "https://www.federalreserve.gov/x", "title": "federalreserve.gov"},)
    events, dropped, duplicates = checked_events(reply, D1, start, end, citations, KEYS)
    assert dropped == 2 and duplicates == 1
    assert [e["title"] for e in events] == [
        "Fed chair remarks",
        "Rocket Lab Q3 earnings",
    ]  # intraday before after-close
    assert events[0]["symbols"] == ["SPY", "MARKET"]
    assert events[0]["sources"] == [
        {"url": "https://www.federalreserve.gov/x", "title": "federalreserve.gov", "cited": True}
    ]
    assert events[1]["sources"][0]["cited"] is False


def test_proposal_mapping():
    burner = ("SPCX", "RKLB")

    def run(*events):
        return propose(events, D1, list(KEYS), burner)

    def ev(**fields):
        base = {
            "symbols": ["RKLB"],
            "type": "earnings",
            "title": "t",
            "date": str(D1),
            "timing": "after_close",
            "time_et": None,
            "impact": "high",
        }
        return {**base, **fields}

    earnings = run(ev(title="RKLB earnings"))
    assert earnings["factors"] == {"RKLB": 1.5, "SPCX": 1.0, "SPY": 1.0} and not earnings["burner_off"]
    assert earnings["reasons"] == {"RKLB": ["RKLB earnings"]}
    cpi = run(ev(symbols=["MARKET"], type="cpi", title="CPI", timing="pre_open"))
    assert cpi["factors"] == {"RKLB": 1.25, "SPCX": 1.25, "SPY": 1.5} and not cpi["burner_off"]
    medium = run(ev(symbols=["MARKET"], type="other_macro", impact="medium", timing="pre_open"))
    assert medium["factors"] == {"RKLB": 1.0, "SPCX": 1.0, "SPY": 1.25}
    fomc = run(ev(symbols=["MARKET"], type="fomc", title="FOMC decision", timing="intraday", time_et="14:00"))
    assert fomc["burner_off"] and fomc["burner_reason"] == ["FOMC decision"]
    assert not run(ev(symbols=["MARKET"], type="fomc", date=str(D2)))["burner_off"]  # not this session
    assert run(ev(type="launch", timing="intraday", time_et="14:30"))["burner_off"]  # a burner symbol, late
    assert not run(ev(type="launch", timing="intraday", time_et="10:30"))["burner_off"]  # early in the day
    assert not run(ev(symbols=["SPY"], type="index_event", timing="intraday", time_et="15:00"))["burner_off"]
    low = run(
        ev(impact="low"),
        ev(symbols=["MARKET"], impact="low", timing="intraday", time_et="15:00", type="fomc"),
    )
    assert low == {
        "factors": {"RKLB": 1.0, "SPCX": 1.0, "SPY": 1.0},
        "burner_off": False,
        "reasons": {},
        "burner_reason": [],
    }


# ------------------------------------------------------------------ the forecast hook
@pytest.mark.asyncio
async def test_nothing_applies_until_approved(tmp_path, monkeypatch, contest_frame):
    m, quotes, snap, now = contest_frame
    reply = google(calendar_body(), ["https://investors.rocketlabusa.com/news/q3"])
    service = make_service(
        tmp_path, monkeypatch, m, lambda r: httpx.Response(200, json=reply), enabled=True, now=now
    )
    plain = ScenarioEngine(m).build(snap, quotes, now)
    calendar = await service.event_risk.refresh("operator", now, D1)
    assert calendar["proposal"]["factors"]["RKLB"] == 1.5
    assert service.event_risk.factors_for(D1) == {}
    before = service.scenarios.build(snap, quotes, now)
    assert (
        before.identifier == plain.identifier and before.source == plain.source and before.event_caution == {}
    )
    state = service.state()["event_risk"]
    assert state["pending_approval"] and state["approval"] is None and state["calendar"]["has_suggestions"]
    approved = service.event_risk.approve(D1, {"RKLB": 1.5}, False, calendar["id"])
    assert approved["approval"]["status"] == "in effect" and not approved["pending_approval"]
    after = service.scenarios.build(snap, quotes, now)
    assert after.event_caution == {"RKLB": 1.5} and after.identifier != plain.identifier
    assert after.source == plain.source + "; event caution RKLB x1.50"
    assert not service.event_risk.matches(before) and service.event_risk.matches(after)
    record = service.store.records("event_caution", m.account, 1)[0]
    assert record["evidence"] == calendar["events"] and record["proposed"] == calendar["proposal"]
    assert (
        record["factors"] == {"RKLB": 1.5, "SPCX": 1.0, "SPY": 1.0}
        and record["calendar_id"] == calendar["id"]
    )
    assert record["expires_at"] == et(D1, 16).isoformat()


def test_caution_scales_forecast_not_stress(contest_frame):
    m, quotes, snap, now = contest_frame
    engine = ScenarioEngine(m)
    base = engine.build(snap, quotes, now)
    engine.event_caution = lambda day: {"RKLB": 1.5} if day == D1 else {}
    cautious = engine.build(snap, quotes, now)
    # The stress grid (and market stress rows) is bit-identical: the 3% cap and its feasibility are unchanged.
    assert np.array_equal(base.stress, cautious.stress)
    rklb = [j for j, i in enumerate(base.ids) if m.contract_map[i].symbol == "RKLB"]
    other = [j for j in range(len(base.ids)) if j not in rklb]
    for name in ("changes", "next_changes", "overnight_changes"):
        a, b = getattr(base, name), getattr(cautious, name)
        assert np.array_equal(a[:, other], b[:, other]), name
        assert all(not np.array_equal(a[:, j], b[:, j]) for j in rklb), name
    stock = base.ids.index(20)
    ratio = cautious.changes[:, stock].std() / base.changes[:, stock].std()
    assert 1.4 < ratio < 1.6  # spot shock x1.5 on the RKLB share column
    np.testing.assert_allclose(cautious.iv_shocks["RKLB"], 1.5 * base.iv_shocks["RKLB"])
    np.testing.assert_array_equal(cautious.iv_shocks["SPY"], base.iv_shocks["SPY"])
    assert cautious.identifier != base.identifier and "event caution RKLB x1.50" in cautious.source
    assert base.event_caution == {} and cautious.event_caution == {"RKLB": 1.5}
    # Factors outside {1.25, 1.5} never enter a build.
    engine.event_caution = lambda day: {"RKLB": 3.0, "SPCX": 1.0}
    assert engine.build(snap, quotes, now).identifier == base.identifier
    gate = RiskGate(m)
    hedged = {**snap.quantities(), 20: 300, 206: 2}
    for quantities in (snap.quantities(), hedged):
        a = gate.validate(snap, quotes, base, quantities, now)
        b = gate.validate(snap, quotes, cautious, quantities, now)
        assert a.metrics["stress_loss"] == b.metrics["stress_loss"]
        assert a.metrics["stress_limit"] == b.metrics["stress_limit"] and a.reasons == b.reasons


def test_spy_caution_covers_index_proxies(tmp_path, monkeypatch):
    now = et(D1, 10)
    daily_history(tmp_path / "daily.csv", now)
    m = contest_manifest(tmp_path / "daily.csv", ivv=True)
    quotes = contest_quotes(m, now)
    snap = contest_snapshot(m, quotes, now)
    service = make_service(tmp_path, monkeypatch, m, now=now)
    assert service.event_risk.expands["SPY"] == ["IVV", "SPY"]
    base = service.scenarios.build(snap, quotes, now)
    service.event_risk.approve(D1, {"SPY": 1.25})
    assert service.event_risk.factors_for(D1) == {"IVV": 1.25, "SPY": 1.25}
    cautious = service.scenarios.build(snap, quotes, now)
    assert cautious.event_caution == {"IVV": 1.25, "SPY": 1.25}
    assert "event caution IVV x1.25 SPY x1.25" in cautious.source
    ivv, spcx = base.ids.index(40), base.ids.index(10)
    assert 1.2 < cautious.changes[:, ivv].std() / base.changes[:, ivv].std() < 1.3
    assert np.array_equal(cautious.changes[:, spcx], base.changes[:, spcx])
    assert np.array_equal(cautious.stress, base.stress)


def test_optimizer_certifies_at_measured_candidate_counts(contest_frame):
    """~12 tradable candidates (docs/CONTEST_RESTRICTIONS.md): the full solve certifies with and without
    the widest caution, and the certified hedge still passes the unchanged 3% stress check."""
    m, quotes, snap, now = contest_frame
    assert len(m.allowlist) == 12
    engine = ScenarioEngine(m)
    base = engine.build(snap, quotes, now)
    engine.event_caution = lambda day: {k: 1.5 for k in KEYS}
    cautious = engine.build(snap, quotes, now)
    gate, optimizer = RiskGate(m), HedgeOptimizer(m)
    for scenarios in (base, cautious):
        started = time.perf_counter()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            target = optimizer.solve(snap, quotes, scenarios, Decimal("1000000"), 1_000_400.0)
        metrics = target.solver_metrics
        assert target.status == "OPTIMAL", (scenarios.source, target.status)
        assert time.perf_counter() - started < m.policy.solver_seconds + 5
        assert (
            metrics["returned_primary_objective"]
            <= metrics["primary_lower_bound"] + m.policy.primary_band + 1e-5
        )
        check = gate.validate(snap, quotes, scenarios, target.quantities, now, 2 * target.estimated_cost)
        assert "Stress loss exceeds 3% budget" not in check.reasons
        assert check.metrics["stress_loss"] <= check.metrics["stress_limit"] + 0.01


# ------------------------------------------------------------------ approval, expiry and continuity
def test_approval_endpoint(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest, now=et(D1, 10))
    client = TestClient(create_app(service))  # no lifespan: nothing connects or trades
    page = client.get("/").text
    frame = re.search(r'<iframe id="events-suggestions"[^>]*>', page).group(0)
    assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in frame and "allow-scripts" not in frame
    token = token_of(client)
    headers = {"X-Control-Token": token}
    body = {"session": str(D1), "factors": {"BA": 1.5}, "burner_off": False, "calendar_id": None}
    assert client.post("/api/event-caution", json=body).status_code == 403
    assert (
        client.post("/api/event-caution", json={**body, "session": str(D2)}, headers=headers).status_code
        == 409
    )
    for bad in ({"BA": 1.1}, {"BA": True}, {"BA": "1.5"}):
        assert (
            client.post("/api/event-caution", json={**body, "factors": bad}, headers=headers).status_code
            == 422
        )
    assert client.post("/api/event-caution", json={**body, "apply": True}, headers=headers).status_code == 422
    unknown = client.post("/api/event-caution", json={**body, "factors": {"XYZ": 1.5}}, headers=headers)
    assert unknown.status_code == 409 and "Unknown caution symbols" in unknown.json()["detail"]
    missing = client.post("/api/event-caution", json={**body, "calendar_id": "nope"}, headers=headers)
    assert missing.status_code == 409
    assert service.store.records("event_caution", manifest.account) == []
    ok = client.post("/api/event-caution", json=body, headers=headers)
    assert ok.status_code == 200
    approval = ok.json()["approval"]
    assert approval["status"] == "in effect" and approval["factors"] == {"BA": 1.5}
    record = service.store.records("event_caution", manifest.account)[0]
    assert record["evidence"] == [] and record["expires_at"] == et(D1, 16).isoformat()
    assert record["expands"] == {"BA": ["BA"]} and record["session"] == str(D1)
    assert service.pending_target is None and service.force_optimize is True
    alerts = [a["message"] for a in service.store.records("alert", manifest.account, 10)]
    assert "Event caution approved for 2026-10-07: BA ×1.50; burner unchanged" in alerts
    state = client.get("/api/state").json()
    assert state["event_risk"]["approval"]["factors"] == {"BA": 1.5} and "events" in state["ai_features"]
    caution = service.qa.context(state)["state"]["event_caution"]
    assert caution == {
        "session": str(D1),
        "factors": {"BA": 1.5},
        "burner_off": False,
        "status": "in effect",
        "pending_approval": False,
    }
    off = client.post("/api/events/refresh", json={}, headers=headers)
    assert off.status_code == 409 and "Turn on AI" in off.json()["detail"]
    assert client.post("/api/events/refresh", json={"force": True}, headers=headers).status_code == 422
    cleared = client.post("/api/event-caution", json={**body, "factors": {"BA": 1.0}}, headers=headers)
    assert cleared.status_code == 200 and service.event_risk.factors_for(D1) == {}


@pytest.fixture
async def operational(manifest, store, quotes, now, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    service.broker.update_quotes(fresh, now)
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), fresh
    service.last_scenarios = service.scenarios.build(service.last_snapshot, fresh, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    task = asyncio.create_task(service._event_loop())
    await service.control("paper")
    yield service
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def risk_now(service):
    snapshot = service.broker.snapshot()
    service.last_risk = service.gate.validate(
        snapshot, service.broker.quotes(), service.last_scenarios, snapshot.quantities(), service.now()
    )
    return service.last_risk


@pytest.mark.asyncio
async def test_approval_never_touches_continuity_and_resume_still_works(operational):
    s = operational
    await s._wait_for_data("Feed interrupted")
    assert s.mode == "WAITING_DATA"
    before = (s.control_epoch, dict(s.data_wait), s.manifest.config_hash, s.profile, s.mode)
    async with s.control_lock:  # as the endpoint does
        s.event_risk.approve(D1, {"BA": 1.5}, True)
    assert (s.control_epoch, dict(s.data_wait), s.manifest.config_hash, s.profile, s.mode) == before
    assert s.pending_target is None and s.force_optimize and s.monitor_wakeup.is_set()
    assert s.store.records("burner_setting", s.manifest.account) == [] and s.burner.override is None
    s.last_scenarios = s.scenarios.build(s.broker.snapshot(), s.broker.quotes(), s.now())
    assert risk_now(s).passed
    await s._resume_after_data_wait()
    assert s.mode == "PAPER" and s.data_wait is None and not s.execution.paused


def test_approval_expires_at_the_session_close(tmp_path, monkeypatch, manifest, snapshot, quotes):
    clock = {"now": et(D1, 10)}
    service = make_service(tmp_path, monkeypatch, manifest)
    service.now = lambda: clock["now"]
    service.event_risk.approve(D1, {"BA": 1.25}, True)
    assert service.event_risk.factors_for(D1) == {"BA": 1.25} and service.event_risk.burner_off(D1)
    during = service.scenarios.build(snapshot, quotes, clock["now"])
    assert during.event_caution == {"BA": 1.25}
    clock["now"] = et(D1, 16, 1)
    after = service.scenarios.build(snapshot, quotes, clock["now"])  # forecasts D2's close now
    assert after.event_caution == {} and "event caution" not in after.source
    assert service.event_risk.matches(after) and service.event_risk.factors_for(D2) == {}
    assert not service.event_risk.burner_off(D1)
    state = service.state()["event_risk"]
    assert state["session"] == str(D2) and state["approval"]["status"] == "expired"
    assert state["approval"]["session"] == str(D1)
    with pytest.raises(ValueError, match="session being forecast now"):
        service.event_risk.approve(D1, {"BA": 1.5})
    # Durable across a restart: the approval is restored, and still applies only to D1's builds.
    restarted = make_service(tmp_path, monkeypatch, manifest, store=service.store, now=et(D1, 11))
    restarted.event_risk.load()
    assert restarted.event_risk.factors_for(D1) == {"BA": 1.25} and restarted.event_risk.burner_off(D1)
    assert restarted.state()["event_risk"]["approval"]["status"] == "in effect"
    restarted.event_risk.approve(D1, {"BA": 1.5}, False)  # same session: the newest approval wins
    restarted.event_risk.approve(D1, {"BA": 1.0}, False)
    again = make_service(tmp_path, monkeypatch, manifest, store=service.store, now=et(D1, 11))
    again.event_risk.load()
    assert again.event_risk.factors_for(D1) == {} and not again.event_risk.burner_off(D1)


@pytest.mark.asyncio
async def test_optimize_discards_a_decision_built_before_a_caution_change(operational, monkeypatch):
    s = operational
    loop = asyncio.get_running_loop()
    original = s.optimizer.solve

    def solve(*args, **kwargs):
        result = original(*args, **kwargs)
        done = threading.Event()

        def approve():
            s.event_risk.approve(D1, {"BA": 1.5})
            s.force_optimize = False  # prove the guard, not the approval, re-arms the optimizer
            done.set()

        loop.call_soon_threadsafe(approve)
        assert done.wait(10)
        return result

    monkeypatch.setattr(s.optimizer, "solve", solve)
    s.force_optimize, s.last_optimization["OPT"] = False, time.monotonic()  # stock-only: fast
    decisions = len(s.store.records("decision", s.manifest.account, 100))
    epoch = s.control_epoch
    await s.optimize()
    assert len(s.store.records("decision", s.manifest.account, 100)) == decisions
    assert s.pending_target is None and s.last_target is None and s.force_optimize
    assert s.control_epoch == epoch


@pytest.mark.asyncio
async def test_decision_records_event_caution(operational):
    s = operational
    s.event_risk.approve(D1, {"BA": 1.5})
    s.last_scenarios = s.scenarios.build(s.broker.snapshot(), s.broker.quotes(), s.now())
    s.force_optimize, s.last_optimization["OPT"] = False, time.monotonic()
    await s.optimize()
    decision = s.store.records("decision", s.manifest.account, 1)[0]
    assert decision["event_caution"] == {"BA": 1.5} and "event caution BA x1.50" in decision["source"]
    exported = s.surplus_snapshot()
    assert exported["scenarios"]["event_caution"] == {"BA": 1.5}
    # The surplus add-on reads the export strictly (extra fields forbidden) and keeps the caution.
    addon = Input.model_validate(json.loads(json.dumps(exported)))
    assert addon.scenarios.array().event_caution == {"BA": 1.5}
    assert s.state()["event_risk"]["in_effect"] == {"BA": 1.5}


@pytest.mark.asyncio
async def test_burner_event_off_is_session_scoped_and_downgrade_only(tmp_path, monkeypatch, manifest, store):
    monkeypatch.chdir(tmp_path)
    data = manifest.model_dump()
    m = Manifest.model_validate(
        {
            **data,
            "policy": {**data["policy"], "burn_surplus": False},
            "burner": {"mode": "paper", "admissibility_source": "test", "symbols": ["BA"]},
        }
    )
    clock = {"now": et(D1, 15, 50)}
    service = TradingService(m, store, demo=True)
    service.now = lambda: clock["now"]
    store.clock = lambda: clock["now"]
    await service.broker.connect()
    task = asyncio.create_task(service._event_loop())

    def refresh():
        now = clock["now"]
        quotes = demo_quotes(m, now)
        service.broker.update_quotes(quotes, now)
        snap = service.broker.snapshot()
        service.last_snapshot, service.last_quotes = snap, quotes
        service.last_scenarios = service.scenarios.build(snap, quotes, now)
        service.last_scenario_at, service.last_scenario_revision = now, service.broker_event_revision
        service.last_risk = service.gate.validate(
            snap, quotes, service.last_scenarios, snap.quantities(), now
        )

    try:
        refresh()
        # NAV below target: no surplus, so the burner monitors and never opens a cycle here.
        service.ledger.import_close(m.day_zero, service.broker.snapshot().nav + 1000, "fixture", m.account)
        await service.control("paper")
        refresh()
        assert service.burner.effective_mode() == "paper" and service.burner.exits_allowed()
        service.event_risk.approve(D1, {"BA": 1.0}, False)  # no burner downgrade: unchanged
        assert service.burner.configured_mode() == "paper"
        waiting = "Waiting for a scenario forecast that includes the latest fills"
        service.event_risk.approve(D1, {"BA": 1.5}, False)  # the forecast set is now stale
        service.monitor_wakeup.clear()
        service.burner._monitor_wake_at = 0.0
        await service.burner.step()
        assert service.burner.reason == waiting and service.monitor_wakeup.is_set()  # asks for a rebuild
        refresh()
        await service.burner.step()
        assert service.burner.reason != waiting and service.last_scenarios.event_caution == {"BA": 1.5}
        service.event_risk.approve(D1, {"BA": 1.0}, True)
        assert service.burner.configured_mode() == service.burner.effective_mode() == "off"
        assert service.burner.state()["event_off"] is True
        await service.burner.step()
        assert service.burner.state_name == "OFF"
        assert service.burner.reason == "Burner off for this session (approved event caution)"
        assert service.burner.exits_allowed()  # an open round trip still sells back what it bought
        assert service.store.records("burner_setting", m.account) == [] and service.burner.override is None
        clock["now"] = et(D2, 15, 50)  # the next session: back to the configured mode
        assert not service.burner.state()["event_off"]
        assert service.burner.configured_mode() == service.burner.effective_mode() == "paper"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    configured_off = Manifest.model_validate(
        {**m.model_dump(), "burner": {**m.burner.model_dump(), "mode": "off"}}
    )
    restarted = TradingService(configured_off, store, demo=True)
    restarted.now = lambda: et(D1, 15, 50)
    restarted.event_risk.load()
    restarted.event_risk.approve(D1, {"BA": 1.0}, False)  # an approval without burner-off never raises it
    assert restarted.burner.configured_mode() == "off" and not restarted.burner.state()["event_off"]


# ------------------------------------------------------------------ search suggestions and schedule
def test_search_suggestions_route_is_sandboxed(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest, now=et(D1, 10))
    snippet = "<style>.c{color:red}</style><div class='c'><a href='https://www.google.com/search?q=rklb'>rklb</a></div>"
    service.event_risk.calendars["cal1"] = {
        "id": "cal1",
        "session": str(D1),
        "status": "ok",
        "search_suggestions_html": snippet,
    }
    service.event_risk.calendars["cal2"] = {
        "id": "cal2",
        "session": str(D1),
        "status": "ok",
        "search_suggestions_html": " ",
    }
    client = TestClient(create_app(service))
    page = client.get("/ai/search-suggestions/cal1")
    assert page.status_code == 200 and snippet in page.text and '<base target="_blank">' in page.text
    csp = page.headers["content-security-policy"]
    assert csp == SUGGESTIONS_CSP and "frame-ancestors 'self'" in csp and "script" not in csp
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in client.get("/api/state").headers["content-security-policy"]
    for missing in ("cal2", "unknown", "x" * 80):
        assert client.get(f"/ai/search-suggestions/{missing}").status_code == 404


@pytest.mark.asyncio
async def test_morning_schedule(tmp_path, monkeypatch, manifest):
    calls = []

    def failing(request):
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={"error": SECRET})

    service = make_service(tmp_path, monkeypatch, manifest, failing, enabled=True)
    clock = {"now": et(D1, 8, 44)}
    service.now = lambda: clock["now"]
    risk = service.event_risk

    async def tick(hour, minute, day=D1):
        clock["now"] = et(day, hour, minute)
        risk.tick(clock["now"])
        await asyncio.gather(*list(service.ai_tasks))

    await tick(8, 44)
    assert calls == []
    await tick(8, 46)
    assert len(calls) == 1 and calls[0]["tools"] == [{"type": "google_search"}]
    calendar = service.state()["event_risk"]["calendar"]
    assert calendar["status"] == "failed" and calendar["error"] == "Google AI request failed (HTTP 500)"
    await tick(8, 50)  # a failure retries 10 minutes later
    assert len(calls) == 1
    await tick(8, 57)
    await tick(9, 8)
    await tick(9, 19)  # at most 3 automatic attempts per day
    assert len(calls) == 3
    alerts = [
        a
        for a in service.store.records("alert", manifest.account, 20)
        if "Event calendar AI unavailable" in a["message"]
    ]
    assert len(alerts) == 1 and SECRET not in json.dumps(
        service.store.records("event_calendar", manifest.account, 20)
    )
    assert risk.start_refresh() == {"status": "started", "session": str(D1)}  # 09:19: the operator's request
    await asyncio.gather(*list(service.ai_tasks))
    clock["now"] = et(D1, 9, 25)
    with pytest.raises(EventsLimit, match="Please wait"):
        risk.start_refresh()
    for minute in (30, 41):
        clock["now"] = et(D1, 9, minute)
        risk.start_refresh()
        await asyncio.gather(*list(service.ai_tasks))
    assert len(calls) == 6
    clock["now"] = et(D1, 10, 0)
    with pytest.raises(EventsLimit, match="6 per day"):
        risk.start_refresh()
    restarted = make_service(
        tmp_path, monkeypatch, manifest, failing, enabled=True, store=service.store, now=clock["now"]
    )
    restarted.event_risk.load()
    with pytest.raises(EventsLimit, match="6 per day"):  # the ET-day count survives a restart
        restarted.event_risk.start_refresh()
    assert restarted.state()["event_risk"]["calls_today"] == 6
    await tick(15, 31, D2)  # after the automatic window
    await tick(8, 46, date(2026, 10, 10))  # Saturday: not a contest session
    assert len(calls) == 6
    await tick(8, 46, D2)  # the next session day starts a fresh count
    assert len(calls) == 7
    monkeypatch.delenv("GEMINI_API_KEY")
    await tick(9, 30, D2)
    assert len(calls) == 7  # AI off: nothing is fetched
    with pytest.raises(EventsBusy, match="Turn on AI"):
        risk.start_refresh()


@pytest.mark.asyncio
async def test_an_ok_calendar_ends_the_mornings_automatic_fetches(tmp_path, monkeypatch, manifest):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(500)
        body = calendar_body(events=[event(symbols=["BA"], title="BA earnings")])
        return httpx.Response(200, json=google(body, ["https://investors.rocketlabusa.com/news/q3"]))

    service = make_service(tmp_path, monkeypatch, manifest, handler, enabled=True, now=et(D1, 8, 46))
    service.event_risk.tick(et(D1, 8, 46))
    await asyncio.gather(*list(service.ai_tasks))
    service.event_risk.tick(et(D1, 9, 30))
    assert len(calls) == 1 and not service.ai_tasks
    state = service.state()["event_risk"]
    assert state["calendar"]["status"] == "ok" and state["pending_approval"]
    assert state["calendar"]["proposal"]["factors"] == {"BA": 1.5}
    assert state["calendar"]["events"][0]["sources"][0]["cited"] is True
    assert service.event_risk.factors_for(D1) == {}  # proposed, not applied
    assert service.state()["ai_features"]["events"]["status"].startswith("Calendar for 2026-10-07: 1 event")
    assert state["latest_failure"] is None
    await service.event_risk.refresh("operator", et(D1, 9, 40), D1)  # a failed refresh keeps the good one
    state = service.state()["event_risk"]
    assert state["calendar"]["status"] == "ok" and state["pending_approval"]
    assert state["latest_failure"]["error"] == "Google AI request failed (HTTP 500)"


@pytest.mark.asyncio
async def test_invalid_replies_are_rejected_and_alerted_once(tmp_path, monkeypatch, manifest):
    replies = [
        google(calendar_body(D2)),  # another session
        google(calendar_body(events=[event(symbols=["BA"], source_urls=[])])),  # high impact, no source
        {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "{"}]}],
        },
    ]

    def handler(request):
        return httpx.Response(200, json=replies.pop(0))

    service = make_service(tmp_path, monkeypatch, manifest, handler, enabled=True, now=et(D1, 9))
    for _ in range(3):
        calendar = await service.event_risk.refresh("operator", et(D1, 9), D1)
        assert calendar["status"] == "failed" and calendar["error"] == "Invalid Google AI response rejected"
        assert calendar["events"] == [] and calendar["proposal"] is None
    alerts = [
        a for a in service.store.records("alert", manifest.account, 20) if "Event calendar AI" in a["message"]
    ]
    assert len(alerts) == 1
    assert service.state()["event_risk"]["pending_approval"] is False

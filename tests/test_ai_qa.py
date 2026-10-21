import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from hedge.ai_common import OTHER_ALERT, RateLimit, operator_text, safe_alerts
from hedge.ai_qa import GUIDE_LIMIT, GUIDE_SECTIONS, POINT_TO, UNCITED, QABusy, QALimit, guide_excerpt
from hedge.config import NY
from hedge.demo import demo_manifest
from hedge.service import TradingService
from hedge.storage import Store
from hedge.supervisor import ADVISORY, extract_grounding, inline_schema
from hedge.webapp import create_app

SECRET = "test-credential-7731"


def reply(answer="IBKR is not connected.", relied_on=("checklist.IBKR", "health"), **extra):
    text = json.dumps({"answer": answer, "relied_on": list(relied_on), "point_to": "TWS (Paper)", **extra})
    return {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_tokens": 42, "private": "x"},
    }


def service_for(tmp_path, monkeypatch, handler=None, enabled=False):
    monkeypatch.chdir(tmp_path)
    if enabled:
        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/qa.db"), demo=True)
    service.store.initialize()

    def forbidden(request):
        raise AssertionError("AI off: Google must not be called")

    service.supervisor.transport = httpx.MockTransport(handler or forbidden)
    return service


def seeded_state(service):
    """Real demo state plus private and raw broker text that must never reach Google."""
    now = service.now()
    state = service.state()
    return {
        **state,
        "account": "DU7654321",
        "health": "Connected to DU7654321; Waiting for broker/data: Error 162 RAWHEALTH",
        "checklist": [
            {"label": "IBKR", "status": "ok", "detail": "Connected to DU7654321 (port 7497)"},
            {
                "label": "Mandated legs",
                "status": "fail",
                "detail": "5 of 7 held. Enter in TWS (Paper): BUY 25 SECRETLEG",
            },
            {
                "label": "Account holdings",
                "status": "fail",
                "detail": "Close these non-contest positions in TWS: 300 LEFTOVER",
            },
            {"label": "Trading", "status": "warn", "detail": "Paused: fix the ✗ rows above"},
        ],
        "alerts": [
            {
                "severity": "error",
                "at": now.isoformat(),
                "message": "Error 201: Order rejected RAWBROKER DU7654321",
            },
            {
                "severity": "warning",
                "at": now.isoformat(),
                "message": "Closing burner step failed: RuntimeError: RAWX",
            },
            {"severity": "warning", "at": (now - timedelta(days=2)).isoformat(), "message": "AI old alert"},
        ],
        "positions": [{"label": "POSLABEL", "quantity": 25, "con_id": 101}],
        "orders": [{"con_id": 101, "signed_quantity": 25, "decision_id": "ORDERX"}],
        "fills": [{"exec_id": "EXECX", "signed_quantity": 25}],
        "market_quotes": [{"label": "QUOTEX", "bid": 1.0}],
        "contract_labels": {"101": "LABELX"},
        "decisions": [
            {
                "at": now.isoformat(),
                "status": "OPTIMAL",
                "trade_eligible": False,
                "improvement": 1.5,
                "threshold": 25.0,
                "blocking_reasons": ["Modeled improvement $1.50 does not exceed threshold $25.00"],
                "explanation": "untrusted narrative",
                "reference_midpoints": {"101": 9.9},
            }
        ],
    }


@pytest.mark.asyncio
async def test_ask_without_ai_answers_from_the_checklist(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch)
    assert not service.supervisor.enabled
    result = await service.qa.ask("Why isn't it trading?")
    assert result["source"] == "deterministic" and result["advisory_only"] is True
    assert "Paper trading is blocked by: IBKR: Not connected" in result["answer"]
    assert "checklist.IBKR" in result["relied_on"] and result["point_to"] == "Today: readiness"
    assert result["note"].startswith("AI is off")
    stored = service.store.records("ai_question", service.manifest.account, 5)
    assert [r["id"] for r in stored] == [result["id"]] and stored[0]["question"] == "Why isn't it trading?"
    assert service.state()["qa_history"][0]["id"] == result["id"]
    assert service.state()["ai_features"]["qa"]["enabled"] is False
    service.qa.load()  # a restart restores the history from the records
    assert service.qa.history()[0]["answer"] == result["answer"]


@pytest.mark.asyncio
async def test_ask_sends_only_the_allowlisted_state(tmp_path, monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == SECRET
        return httpx.Response(200, json=reply())

    service = service_for(tmp_path, monkeypatch, handler, enabled=True)
    state = seeded_state(service)
    service.state = lambda: state
    result = await service.qa.ask("Is DU7654321 trading? Enter in TWS (Paper): BUY 99 QUESTIONLEG")
    assert result["source"] == "ai" and result["answer"] == "IBKR is not connected."
    assert result["relied_on"] == ["checklist.IBKR", "health"] and result["model"] == "gemini-3.8-flash"
    body = sent[0]
    assert body["tools"] == [] and body["store"] is False and body["background"] is False
    assert body["system_instruction"].endswith(ADVISORY)
    raw = body["input"]
    for private in (
        "DU7654321", "SECRETLEG", "LEFTOVER", "RAWBROKER", "RAWX", "RAWHEALTH", "POSLABEL", "ORDERX",
        "EXECX", "QUOTEX", "LABELX", "untrusted", "reference_midpoints", "QUESTIONLEG", "AI old alert",
        "DU_SYNTHETIC", SECRET,
    ):  # fmt: skip
        assert private not in raw, private
    data = json.loads(raw)
    assert set(data) == {"question", "now_et", "state_keys", "state", "guide"}
    assert data["question"] == "Is [account] trading? Enter in TWS (Paper): [details on the dashboard]"
    assert not {"account", "positions", "orders", "fills", "market_quotes", "contract_labels"} & set(
        data["state"]
    )
    rows = {r["label"]: r["detail"] for r in data["state"]["checklist"]}
    assert rows["Mandated legs"] == "5 of 7 held. Enter in TWS (Paper): [details on the dashboard]"
    assert rows["Account holdings"] == "Non-contest positions must be closed in TWS (listed on the dashboard)"
    assert rows["IBKR"] == "Connected to [account] (port 7497)"
    assert [a["text"] for a in data["state"]["alerts"]] == [
        OTHER_ALERT,
        "Closing burner step failed: [details on the dashboard]",
    ]
    assert data["state"]["decisions"][0]["blocking_reasons"] == [
        "Modeled improvement $1.50 does not exceed threshold $25.00"
    ]
    assert "checklist.Mandated legs" in data["state_keys"] and "guide" in data["state_keys"]
    assert "## Today: readiness (top of Overview)" in data["guide"]


@pytest.mark.asyncio
async def test_unknown_refs_are_dropped_and_extra_fields_rejected(tmp_path, monkeypatch):
    replies = [
        reply(relied_on=["checklist.IBKR", "positions", "account"]),
        reply(relied_on=["positions"]),
        reply(orders=[{"buy": 1}]),
        reply(point_to="Place an order"),
    ]
    service = service_for(tmp_path, monkeypatch, lambda r: httpx.Response(200, json=replies.pop(0)), True)
    service.qa.interval = RateLimit()
    first = await service.qa.ask("why?")
    assert first["relied_on"] == ["checklist.IBKR"] and first["note"] is None
    uncited = await service.qa.ask("why?")
    assert uncited["source"] == "ai" and uncited["relied_on"] == [] and uncited["note"] == UNCITED
    for _ in range(2):  # an extra field or an off-menu dashboard place is rejected
        rejected = await service.qa.ask("why?")
        assert rejected["source"] == "deterministic"
        assert "Invalid Google AI response rejected" in rejected["note"]


@pytest.mark.asyncio
async def test_question_rate_limits_and_busy(tmp_path, monkeypatch):
    release = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            await release.wait()
        return httpx.Response(200, json=reply())

    service = service_for(tmp_path, monkeypatch, handler, enabled=True)
    clock = [1000.0]
    service.qa.interval.clock = service.qa.hourly.clock = lambda: clock[0]
    first = asyncio.create_task(service.qa.ask("first"))
    while not calls:
        await asyncio.sleep(0.01)
    with pytest.raises(QABusy):
        await service.qa.ask("concurrent")
    release.set()
    assert (await first)["source"] == "ai"
    clock[0] += 5
    with pytest.raises(QALimit, match="Please wait 5 s"):
        await service.qa.ask("too soon")
    for _ in range(59):
        clock[0] += 10
        assert (await service.qa.ask("again"))["source"] == "ai"
    clock[0] += 10
    capped = await service.qa.ask("61st")
    assert capped["source"] == "deterministic" and "60 per hour" in capped["note"]
    assert len(calls) == 60
    clock[0] += 3600
    assert (await service.qa.ask("next hour"))["source"] == "ai"


def test_ask_requires_the_control_token(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch)
    with TestClient(create_app(service)) as client:
        page = client.get("/").text
        assert 'id="ask-panel"' in page and 'id="ai-features"' in page
        token = re.search(r'name="control-token" content="([^"]+)"', page).group(1)
        assert client.post("/api/ask", json={"question": "Why?"}).status_code == 403
        assert service.store.records("ai_question", service.manifest.account, 5) == []
        headers = {"X-Control-Token": token}
        assert client.post("/api/ask", json={"question": "  "}, headers=headers).status_code == 422
        assert client.post("/api/ask", json={"question": "x" * 501}, headers=headers).status_code == 422
        assert (
            client.post("/api/ask", json={"question": "Why?", "act": 1}, headers=headers).status_code == 422
        )
        answer = client.post("/api/ask", json={"question": " Why? "}, headers=headers)
        assert answer.status_code == 200 and answer.json()["question"] == "Why?"
        assert answer.json()["source"] == "deterministic"
        again = client.post("/api/ask", json={"question": "Why?"}, headers=headers)
        assert again.status_code == 429 and "Please wait" in again.json()["detail"]

        async def busy(question):
            raise QABusy("Still answering the previous question")

        service.qa.ask = busy
        assert client.post("/api/ask", json={"question": "Why?"}, headers=headers).status_code == 409


@pytest.mark.asyncio
async def test_failure_is_sanitized_and_alerted_once(tmp_path, monkeypatch):
    service = service_for(
        tmp_path, monkeypatch, lambda r: httpx.Response(500, text=f"key {SECRET} provider echo"), True
    )
    service.qa.interval = RateLimit()
    first = await service.qa.ask("why?")
    second = await service.qa.ask("why?")
    for result in (first, second):
        assert result["source"] == "deterministic" and "HTTP 500" in result["note"]
    status = service.qa.status()
    assert status["last_error"] == "Google AI request failed (HTTP 500)" and "HTTP 500" in status["status"]
    alerts = service.store.records("alert", service.manifest.account, 10)
    assert len(alerts) == 1 and alerts[0]["message"].startswith("AI questions unavailable")
    everything = json.dumps([first, second, status, alerts, service.state()["ai_features"]])
    assert SECRET not in everything and "provider echo" not in everything


@pytest.mark.asyncio
async def test_timeout_falls_back(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch, enabled=True)

    async def slow(*args, **kwargs):
        raise TimeoutError

    service.supervisor.structured = slow
    result = await service.qa.ask("why?")
    assert result["source"] == "deterministic" and "Google AI timed out" in result["note"]


@pytest.mark.asyncio
async def test_ask_never_changes_trading_state(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch, lambda r: httpx.Response(200, json=reply()), True)
    await service.start()
    try:
        service.supervisor.last_call = time.monotonic()  # no scheduled review during the test
        await asyncio.sleep(0.1)

        def trading():
            return (
                service.mode,
                service.profile,
                service.control_epoch,
                service.autostart_mode(),
                service.autostart_pending,
                service.burner.override,
                service.data_wait,
                service.pending_target,
                service.execution.paused,
                service.broker.snapshot().quantities(),
                len(service.store.intents(service.manifest.account)),
            )

        before = trading()
        assert (await service.qa.ask("Why isn't it trading?"))["source"] == "ai"
        assert trading() == before
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_stop_cancels_background_ai_work(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch)
    await service.start()
    task = service.spawn_ai(asyncio.sleep(3600), "ai-test")
    assert task in service.ai_tasks
    await service.stop()
    assert task.cancelled() and not service.ai_tasks


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=500)


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Inner]
    best: Inner | None = None


@pytest.mark.asyncio
async def test_structured_inlines_schema_and_extracts_grounding(tmp_path, monkeypatch):
    schema = inline_schema(Outer)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    assert schema["properties"]["items"]["items"]["properties"]["url"]["maxLength"] == 500
    body = {
        "status": "completed",
        "steps": [
            {"type": "google_search_call", "id": "c1", "arguments": {"queries": ["RKLB earnings date", 7]}},
            {
                "type": "google_search_result",
                "call_id": "c1",
                "result": [{"search_suggestions": "<div>chips</div>"}, {"search_suggestions": "x" * 30_000}],
            },
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"items": [{"url": "https://a.example/x"}]}),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://a.example/x",
                                "title": "A",
                                "start_index": 0,
                            },
                            {"type": "url_citation", "url": "https://a.example/x", "title": "dup"},
                            {"type": "url_citation", "url": "http://insecure.example", "title": "B"},
                            {"type": "other", "url": "https://c.example"},
                        ],
                    }
                ],
            },
        ],
        "usage": {"total_tokens": 9},
    }
    citations, queries, suggestions = extract_grounding(body)
    assert citations == ({"url": "https://a.example/x", "title": "A"},)
    assert queries == ("RKLB earnings date",) and suggestions == "<div>chips</div>"
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    service = service_for(tmp_path, monkeypatch, handler, enabled=True)
    result = await service.supervisor.structured(
        Outer, instructions="Test.", payload={"a": 1}, tools=[{"type": "google_search"}], timeout=5
    )
    assert result.output.items[0].url == "https://a.example/x" and result.citations == citations
    assert result.usage == {"total_input_tokens": None, "total_output_tokens": None, "total_tokens": 9}
    assert sent[0]["tools"] == [{"type": "google_search"}] and sent[0]["response_format"]["schema"] == schema
    assert sent[0]["system_instruction"] == f"Test. {ADVISORY}" and sent[0]["store"] is False
    service.supervisor.active = False
    with pytest.raises(ValueError, match="AI disabled"):
        await service.supervisor.structured(Outer, instructions="Test.", payload={})
    assert len(sent) == 1


def test_operator_text_and_alert_filter():
    text = "Connected to U1234567 and DU_SYNTHETIC\x00\n  then Waiting for broker/data: raw 201 text"
    assert operator_text(text, "DU_SYNTHETIC") == (
        "Connected to [account] and [account] then Waiting for broker/data: [details on the dashboard]"
    )
    assert operator_text("x" * 300, None, 10) == "x" * 9 + "…"
    now = demo_manifest().session_close(demo_manifest().scoring_sessions[0])
    alerts = [
        {"severity": "error", "at": now.isoformat(), "message": "Cannot reach TWS for DU1234567"},
        {"severity": "bogus", "at": now.isoformat(), "message": "Order 12 rejected by broker"},
        {"severity": "info", "at": (now - timedelta(hours=25)).isoformat(), "message": "Closing NAV old"},
    ]
    assert safe_alerts(alerts, "DU1234567", now) == [
        {"severity": "error", "at": now.isoformat(), "text": "Cannot reach TWS for [account]"},
        {"severity": "warning", "at": now.isoformat(), "text": OTHER_ALERT},
    ]


def test_guide_excerpt_keeps_only_allowlisted_sections():
    excerpt = guide_excerpt()
    headings = re.findall(r"(?m)^## (.+)$", excerpt)
    assert headings == list(GUIDE_SECTIONS) and len(excerpt) <= GUIDE_LIMIT
    assert excerpt.rstrip().endswith("you can still approve by\n  hand.")  # nothing cut off
    assert "Start and stop" not in excerpt


RAW = "IBKR-RAW-WARNING: order may be routed to a different exchange (reqId 42)"


@pytest.mark.asyncio
async def test_execution_and_burner_reasons_never_send_broker_text(tmp_path, monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    service = service_for(tmp_path, monkeypatch, handler, enabled=True)
    service.qa.interval = RateLimit()
    # Exactly as execution.py, service.py and burner_service.py write them after an IBKR what-if refusal.
    service.health = f"Credit/risk preflight blocked: Broker credit preview warning: {RAW}"
    service.burner.reason = (
        f"Entry not transmitted: Burn credit preview blocked: Broker credit preview warning: {RAW}"
    )
    message = f"Closing burner cycle abcd1234 ended RESIDUAL: Burn credit preview blocked: {RAW}; 0 BA left"
    service.alert("error", message, key=("residual",))
    await service.qa.ask("Why isn't it trading?")
    service.burner.state_name, service.burner.reason = "ERROR", f"RuntimeError: {RAW}"  # _burner_loop
    service.health = f"Fresh execution validation: {RAW}"
    await service.qa.ask("And the burner?")
    assert len(sent) == 2
    for body in sent:
        assert "IBKR-RAW" not in body["input"] and "reqId" not in body["input"]
    first, second = (json.loads(body["input"])["state"] for body in sent)
    assert first["health"] == "Credit/risk preflight blocked: [details on the dashboard]"
    assert first["burner"]["reason"] == (
        "Entry not transmitted: Burn credit preview blocked: [details on the dashboard]"
    )
    assert "Closing burner cycle abcd1234 ended RESIDUAL: [details on the dashboard]" in [
        a["text"] for a in first["alerts"]
    ]
    assert second["health"] == "Fresh execution validation: [details on the dashboard]"
    assert second["burner"]["reason"] == "Closing burner step failed: [details on the dashboard]"


@pytest.mark.asyncio
async def test_a_reply_after_ai_is_turned_off_is_dropped(tmp_path, monkeypatch):
    holder = []

    def handler(request):
        holder[0].supervisor.configure(enabled=False)  # the operator presses Disable AI meanwhile
        return httpx.Response(200, json=reply(answer="AI text that must not be shown"))

    service = service_for(tmp_path, monkeypatch, handler, enabled=True)
    holder.append(service)
    result = await service.qa.ask("Why isn't it trading?")
    assert not service.supervisor.enabled
    assert result["source"] == "deterministic" and "AI text" not in result["answer"]
    assert result["note"].startswith("AI is off") and service.qa.status()["last_error"] is None
    assert not service.store.records("alert", service.manifest.account, 10)


def qa_state(mode, checklist, decisions=(), clock=None, health="Monitoring: no qualifying trade"):
    return {
        "mode": mode,
        "health": health,
        "checklist": checklist,
        "decisions": list(decisions),
        "continuity": {},
        "session_clock": clock
        or {
            "session": "2026-10-22",
            "minutes_to_close": 200.0,
            "options_tradable": True,
            "stocks_tradable": True,
        },
    }


FAILS = [
    {"label": "IBKR", "status": "fail", "detail": "Not connected: open TWS (Paper)"},
    {"label": "Previous official close", "status": "fail", "detail": "Missing: enter the closing NAV"},
    {"label": "Quotes", "status": "warn", "detail": "No quotes yet"},
]


def decision(at, reason="Modeled improvement $3.10 does not exceed threshold $100.00"):
    return {"at": at.isoformat(), "trade_eligible": False, "blocking_reasons": [reason]}


def test_fallback_names_failing_rows_first_and_only_recent_decisions(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch)
    now = datetime(2026, 10, 22, 11, 0, tzinfo=NY)
    service.now = lambda: now
    qa = service.qa
    # PAPER with ✗ rows and yesterday's decision: the ✗ rows lead; the old decision is not blamed.
    result = qa.fallback(qa_state("PAPER", FAILS, [decision(now - timedelta(days=1))]))
    assert result["answer"].startswith(
        "Paper trading is on, but these checks fail: IBKR: Not connected: open TWS (Paper); "
        "Previous official close: Missing: enter the closing NAV. Also check: Quotes: No quotes yet."
    )
    assert "decision" not in result["answer"]
    assert {"checklist.IBKR", "checklist.Previous official close"} <= set(result["relied_on"])
    assert "decisions" not in result["relied_on"]
    ok_rows = [{"label": "IBKR", "status": "ok", "detail": "Connected"}]
    recent = qa.fallback(qa_state("PAPER", ok_rows, [decision(now - timedelta(minutes=2))]))
    assert recent["answer"].startswith("The latest decision (Oct 22 10:58 ET) did not trade: Modeled")
    assert "decisions" in recent["relied_on"]
    old = qa.fallback(qa_state("PAPER", ok_rows, [decision(now - timedelta(minutes=20))]))
    assert old["answer"].startswith(
        "Paper trading is on; no hedge decision in the last 5 minutes (the last one: Oct 22 10:40 ET)."
    )
    # After the bell nothing is tradable: say so first, never blame the 15:56 decision.
    now = datetime(2026, 10, 22, 16, 40, tzinfo=NY)
    closed = {
        "session": "2026-10-22",
        "minutes_to_close": -40.0,
        "options_tradable": False,
        "stocks_tradable": False,
    }
    late = qa.fallback(qa_state("PAPER", ok_rows, [decision(now - timedelta(minutes=44))], closed))
    assert late["answer"].startswith("The market is closed for today") and "15:56" not in late["answer"]
    assert "session_clock" in late["relied_on"]
    now = datetime(2026, 10, 22, 8, 0, tzinfo=NY)
    early = {**closed, "minutes_to_close": 480.0}
    assert qa.fallback(qa_state("PAUSED", FAILS, clock=early))["answer"].startswith(
        "The market is not open yet: hedge orders start after the open. Paper trading is blocked by: IBKR"
    )


def test_fallback_says_health_once_with_full_stops(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch)
    health = "Connected; paper execution remains disabled until explicitly enabled"
    rows = [
        {"label": "Quotes", "status": "warn", "detail": "No quotes yet"},
        {"label": "Trading", "status": "warn", "detail": f"Paused: {health}"},
    ]
    answer = service.qa.fallback(qa_state("PAUSED", rows, health=health))["answer"]
    assert answer == f"Paused: {health}. Also check: Quotes: No quotes yet."
    other = service.qa.fallback(qa_state("PAUSED", rows[:1], health="Monitoring"))["answer"]
    assert other == "Paused: Monitoring. Also check: Quotes: No quotes yet."


def test_point_to_names_places_the_dashboard_shows():
    page = (Path(__file__).parents[1] / "hedge/web/index.html").read_text()
    for place in (
        "Event risk today",
        "Day-end miss explanation",
        "Closing surplus burner",
        "Today: readiness",
    ):
        assert place in POINT_TO and f"<h3>{place}</h3>" in page
    assert "Day-end report" not in POINT_TO and "Event risk" not in POINT_TO

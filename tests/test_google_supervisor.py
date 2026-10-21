import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from hedge.demo import demo_manifest
from hedge.service import TradingService
from hedge.storage import Store
from hedge.supervisor import MODELS, Supervisor
from hedge.webapp import create_app


def state():
    return {
        "snapshot_id": "abc",
        "mode": "PAUSED",
        "account": "PRIVATE",
        "connected": True,
        "ready": False,
        "quote_count": 0,
        "health": "private broker string",
        "scores": {"scoring_rule": "pending_interest_adjustment"},
        "market_data_errors": [{"message": "private"}],
        "positions": [{"quantity": 25}],
        "decisions": [{"status": "BLOCKED", "explanation": "untrusted"}],
    }


def response(profile="STANDARD", snapshot="abc", **extra):
    text = json.dumps(
        {"snapshot_id": snapshot, "profile": profile, "explanation": "Missing quotes.", **extra}
    )
    return {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_tokens": 20},
    }


@pytest.mark.asyncio
async def test_google_payload_privacy_tools_and_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "test-credential"
        assert "test-credential" not in str(request.url)
        assert payload["model"] == "gemini-3.8-flash"
        assert payload["tools"] == [] and payload["store"] is False
        assert payload["background"] is False
        safe = json.loads(payload["input"])
        assert "PRIVATE" not in payload["input"] and "private" not in payload["input"]
        assert "untrusted" not in payload["input"] and "positions" not in safe
        calls.append(payload)
        return httpx.Response(200, json=response())

    supervisor = Supervisor(transport=httpx.MockTransport(handler), settings_path=tmp_path / "ai.json")
    result = await supervisor.review(state())
    assert result["model"] == MODELS[0] and result["advisory_only"]
    assert "test-credential" not in json.dumps(result) + json.dumps(supervisor.status())
    assert await supervisor.review(state()) is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        response(profile="GREEK_NEUTRAL"),
        response(snapshot="old"),
        response(positions={"BA": 100}),
        {"status": "requires_action", "steps": []},
    ],
)
async def test_unvalidated_or_trade_outputs_rejected(tmp_path, monkeypatch, body):
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    supervisor = Supervisor(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
        settings_path=tmp_path / "ai.json",
    )
    with pytest.raises(ValueError):
        await supervisor.review(state())
    assert await supervisor.review(state()) is None


@pytest.mark.asyncio
async def test_google_failure_never_echoes_secret_or_response(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    supervisor = Supervisor(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="test-credential")),
        settings_path=tmp_path / "ai.json",
    )
    with pytest.raises(ValueError, match="HTTP 401") as error:
        await supervisor.review(state())
    assert "test-credential" not in str(error.value) + str(supervisor.status())


def test_private_key_and_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    supervisor = Supervisor(settings_path=tmp_path / "ai.json")
    supervisor.key_path = tmp_path / "key"
    supervisor.key_path.write_text("test-credential")
    supervisor.key_path.chmod(0o644)
    assert not supervisor.enabled
    supervisor.key_path.chmod(0o600)
    assert supervisor.enabled
    supervisor.configure(MODELS[1], False)
    assert not supervisor.enabled
    assert json.loads((tmp_path / "ai.json").read_text()) == {"model": MODELS[1], "enabled": False}
    with pytest.raises(ValueError):
        supervisor.configure("arbitrary-URL", True)


@pytest.mark.asyncio
async def test_service_review_while_paused_leaves_execution_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/ai.db"), demo=True)
    await service.start()
    try:

        async def fake_review(value):
            return {"snapshot_id": value["snapshot_id"], "profile": "STANDARD", "explanation": "Read only."}

        service.supervisor.review = fake_review
        await asyncio.sleep(0.05)
        before = service.mode, service.profile, service.broker.snapshot().quantities()
        await service._review()
        assert (service.mode, service.profile, service.broker.snapshot().quantities()) == before
        assert service.store.records("supervisor", service.manifest.account, 1)
    finally:
        await service.stop()


def test_model_selector_route_does_not_enable_trading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-credential")
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/ui.db"), demo=True)
    service.supervisor.active = False
    with TestClient(create_app(service)) as client:
        import re

        page = client.get("/").text
        assert 'id="ai-model"' in page
        token = re.search(r'name="control-token" content="([^"]+)"', page).group(1)
        before = service.mode
        assert client.post("/api/ai/settings", json={"model": MODELS[1], "enabled": False}).status_code == 403
        result = client.post(
            "/api/ai/settings",
            json={"model": MODELS[1], "enabled": False},
            headers={"X-Control-Token": token},
        )
        assert result.status_code == 200 and result.json()["model"] == MODELS[1]
        assert service.mode == before


def test_prompt_and_context_follow_the_configured_scoring_rule():
    calendar = {
        **state(),
        "scores": {
            "scoring_rule": "calendar_interest_benchmark",
            "annual_rate": "0.0344",
            "total": "0",
            "days": [{"date": "2026-10-22", "score": None}],
        },
        "nav_estimate": 1000049.81,
        "closing_target": "1000095.56",
        "provisional_score": 45.75,
        "session_clock": {"minutes_to_close": 12.5, "accrual_days": 1, "oldest_option_quote_age_s": 75.0},
        "risk": {"passed": False, "reasons": ["Stale/future quote: 101"], "metrics": {}},
    }
    supervisor = Supervisor()
    prompt = supervisor.instructions(calendar)
    assert "pending" not in prompt and "0.0344/360)^n_i" in prompt
    assert "pending" in supervisor.instructions(state())
    safe = Supervisor.context(calendar)
    assert safe["official_score"] is None  # unscored, not a perfect zero
    assert safe["closing_target"] == "1000095.56" and safe["provisional_score"] == 45.75
    assert safe["session_clock"]["oldest_option_quote_age_s"] == 75.0
    assert safe["risk_reasons"] == ["Stale/future quote: 101"]
    assert "PRIVATE" not in json.dumps(safe) and "private" not in json.dumps(safe)
    assert "importing that statement" in prompt and safe["api_close_official"] is None
    # SPCX/RKLB/SPY contest: the IBKR API close is the official scoring close, never "import a statement".
    official = {**calendar, "previous_close_provenance": "api", "api_close_official": True}
    prompt = supervisor.instructions(official)
    assert "official scoring close" in prompt and "import" not in prompt.lower()
    assert Supervisor.context(official)["api_close_official"] is True


@pytest.mark.asyncio
async def test_review_is_not_stale_when_only_the_snapshot_timestamp_moves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/ai.db"), demo=True)
    await service.start()
    try:
        await asyncio.sleep(0.05)

        async def slow_review(value):
            # The monitor re-stamps the snapshot while the provider call is in flight.
            service.last_snapshot = service.last_snapshot.model_copy(
                update={"at": service.last_snapshot.at.replace(microsecond=1)}
            )
            return {"snapshot_id": value["snapshot_id"], "profile": "STANDARD", "explanation": "ok"}

        service.supervisor.review = slow_review
        await service._review()
        assert service.store.records("supervisor", service.manifest.account, 1)[0]["stale"] is False
        assert service.state()["session_clock"]
        assert service._ai_reviews()[0]["stale"] is False
        service.last_snapshot = service.last_snapshot.model_copy(
            update={"nav": service.last_snapshot.nav + 1}
        )
        assert service._ai_reviews()[0]["stale"] is True  # account changed after the review
    finally:
        await service.stop()

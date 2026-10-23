"""Claude Sonnet 5.5 (medium effort) as the app's advisory AI: request shape, web search for the
event calendar, failures as fixed texts, the workspace header and private local key storage."""

import json
import os
import stat
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from hedge.supervisor import (
    ADVISORY,
    CLAUDE_FALLBACK_BETA,
    CLAUDE_WEB_SEARCH,
    MODELS,
    Recommendation,
    Supervisor,
)

KEY = "sk-ant-test-credential"


class Calendar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str
    events: list[str]


def text(value, citations=()):
    return SimpleNamespace(type="text", text=value, citations=list(citations))


def reply(content, stop="end_turn", parsed=None, model="claude-sonnet-5-5"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop,
        parsed_output=parsed,
        model=model,
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def parse(self, **request):
        self.calls.append(("parse", request))
        return self.replies.pop(0)

    async def create(self, **request):
        self.calls.append(("create", request))
        return self.replies.pop(0)


def claude(tmp_path, replies, monkeypatch):
    monkeypatch.delenv("HEDGE_AI_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    messages = FakeMessages(replies)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return Supervisor(settings_path=tmp_path / "ai.json", claude_client=client), messages


def test_claude_sonnet_medium_is_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HEDGE_AI_MODEL", raising=False)
    supervisor = Supervisor(settings_path=tmp_path / "ai.json")
    supervisor.claude_key_path = tmp_path / "missing"
    status = supervisor.status()
    assert MODELS[0] == "claude-sonnet-5-5" and status["model"] == "claude-sonnet-5-5"
    assert status["provider"] == "Anthropic Claude" and status["effort"] == "medium"
    assert not status["configured"] and not supervisor.enabled
    with pytest.raises(ValueError, match="Setup Claude AI.command"):
        supervisor.configure(enabled=True)


async def test_a_structured_call_uses_sonnet_at_medium_effort_with_a_validated_reply(tmp_path, monkeypatch):
    advice = Recommendation(snapshot_id="abc", profile="STANDARD", explanation="Missing quotes.")
    supervisor, messages = claude(
        tmp_path, [reply([text(advice.model_dump_json())], parsed=advice)], monkeypatch
    )
    result = await supervisor.structured(Recommendation, instructions="Review.", payload={"x": 1})
    assert result.output == advice and result.model == "claude-sonnet-5-5"
    assert result.usage["total_tokens"] == 150 and result.citations == ()
    kind, request = messages.calls[0]
    assert kind == "parse" and request["model"] == "claude-sonnet-5-5"
    assert request["output_config"] == {"effort": "medium"} and request["output_format"] is Recommendation
    assert request["fallbacks"] == "default" and request["betas"] == [CLAUDE_FALLBACK_BETA]
    assert request["system"].endswith(ADVISORY) and request["max_tokens"] == 16_000
    assert json.loads(request["messages"][0]["content"]) == {"x": 1}
    assert "tools" not in request


async def test_the_calendar_searches_the_web_and_keeps_https_citations(tmp_path, monkeypatch):
    found = SimpleNamespace(
        type="web_search_tool_result",
        content=[
            SimpleNamespace(type="web_search_result", url="https://www.federalreserve.gov/x", title="FOMC"),
            SimpleNamespace(type="web_search_result", url="http://insecure.example/y", title="no"),
        ],
    )
    asked = SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "FOMC Oct 2026"})
    cited = SimpleNamespace(type="web_search_result_location", url="https://www.bls.gov/cpi", title="CPI")
    paused = reply([text("Searching."), asked, found], stop="pause_turn")
    final = reply([text('Here it is: {"session": "2026-10-23", "events": ["FOMC"]}', [cited])])
    supervisor, messages = claude(tmp_path, [paused, final], monkeypatch)
    result = await supervisor.structured(
        Calendar, instructions="List events.", payload={}, tools=[{"type": "google_search"}]
    )
    assert result.output == Calendar(session="2026-10-23", events=["FOMC"])
    assert [c["url"] for c in result.citations] == [
        "https://www.federalreserve.gov/x",
        "https://www.bls.gov/cpi",
    ]
    assert result.search_queries == ("FOMC Oct 2026",) and result.search_suggestions == ""
    (first, request), (_, resumed) = messages.calls
    assert first == "create" and request["tools"] == [CLAUDE_WEB_SEARCH]
    assert "output_config" in request and "format" not in request["output_config"]
    assert "JSON schema" in request["system"] and '"events"' in request["system"]
    assert resumed["messages"][-1]["role"] == "assistant"  # the paused turn sent back to continue


@pytest.mark.parametrize(
    "answer,error",
    [
        (reply([text("{}")], stop="refusal"), "Claude AI did not return a completed answer"),
        (reply([text('{"session": 1}')]), "Invalid Claude AI response rejected"),
        (reply([text("no json")]), "Invalid Claude AI response rejected"),
    ],
)
async def test_refusals_and_invalid_replies_are_fixed_texts(tmp_path, monkeypatch, answer, error):
    supervisor, _ = claude(tmp_path, [answer], monkeypatch)
    with pytest.raises(ValueError) as caught:
        await supervisor.structured(Calendar, instructions="x", payload={}, tools=[{"type": "google_search"}])
    assert str(caught.value) == error


async def test_an_http_failure_never_echoes_the_key_or_the_provider_text(tmp_path, monkeypatch):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    failure = anthropic.AuthenticationError(
        f"bad key {KEY}", response=httpx2.Response(401, request=request), body=None
    )

    class Failing(FakeMessages):
        async def parse(self, **request):
            raise failure

    supervisor, _ = claude(tmp_path, [], monkeypatch)
    supervisor.claude_client = SimpleNamespace(beta=SimpleNamespace(messages=Failing([])))
    with pytest.raises(ValueError) as caught:
        await supervisor.structured(Recommendation, instructions="x", payload={})
    assert str(caught.value) == "Claude AI request failed (HTTP 401)" and KEY not in str(caught.value)


def test_a_key_without_a_workspace_sends_the_workspace_header(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    client = Supervisor(model="claude-sonnet-5-5", settings_path=tmp_path / "ai.json")._claude_client(30)
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_test"
    assert client.api_key == KEY


def test_the_setup_script_stores_the_key_privately_and_selects_claude(tmp_path, monkeypatch):
    import io

    from ops.setup_claude_ai import main

    (tmp_path / "config").mkdir()
    (tmp_path / "config/local.ai.json").write_text(json.dumps({"model": "gemini-3.8-flash", "enabled": True}))
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    main(["--key-stdin", "--workspace", "wrkspc_test", "--root", str(tmp_path)])
    for name, value in (("anthropic-api-key", KEY), ("anthropic-workspace-id", "wrkspc_test")):
        path = tmp_path / ".runtime" / name
        assert path.read_text().strip() == value and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    settings = json.loads((tmp_path / "config/local.ai.json").read_text())
    assert settings == {"model": "claude-sonnet-5-5", "enabled": True}
    supervisor = Supervisor(settings_path=tmp_path / "config/local.ai.json")
    supervisor.claude_key_path = tmp_path / ".runtime/anthropic-api-key"
    supervisor.claude_workspace_path = tmp_path / ".runtime/anthropic-workspace-id"
    assert supervisor.model == "claude-sonnet-5-5" and supervisor.enabled
    assert supervisor._workspace() == "wrkspc_test"
    with pytest.raises(ValueError, match="sk-ant-"):
        monkeypatch.setattr("sys.stdin", io.StringIO("not-a-key\n"))
        main(["--key-stdin", "--root", str(tmp_path)])

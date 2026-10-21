import asyncio
import re

import pytest

from hedge.config import Manifest
from hedge.scenarios import ScenarioEngine
from hedge.service import TradingService


def autostarting(manifest, mode="paper"):
    return Manifest.model_validate({**manifest.model_dump(), "autostart": mode})


@pytest.fixture
async def launched(manifest, store, quotes, now):
    """A freshly launched demo service (PAUSED) whose configuration starts paper automatically."""
    service = TradingService(autostarting(manifest), store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    service.broker.update_quotes(fresh, now)
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), fresh
    service.last_scenarios = ScenarioEngine(service.manifest).build(service.last_snapshot, fresh, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    service.restore_autostart()
    task = asyncio.create_task(service._event_loop())
    yield service
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def risk_now(service):
    snapshot = service.broker.snapshot()
    service.last_risk = service.gate.validate(
        snapshot, service.broker.quotes(), service.last_scenarios, snapshot.quantities(), service.now()
    )
    return service.last_risk


async def test_launch_starts_paper_through_the_checked_activation_path(launched):
    service = launched
    assert service.mode == "PAUSED" and service.autostart_pending
    service._autostart_step([])
    assert service.mode == "WAITING_DATA" and service.data_wait["autostart"]
    assert service.execution.paused and service.broker.readonly  # nothing trades before the checks
    assert service.state()["autostart"] == {
        "mode": "paper",
        "configured": "paper",
        "source": "configuration",
        "pending": False,
        "active": True,
    }
    assert risk_now(service).passed
    await service._resume_after_data_wait()
    assert service.mode == "PAPER" and not service.execution.paused and not service.broker.readonly
    statuses = [r["status"] for r in service.store.records("continuity", service.manifest.account)]
    assert statuses[:2] == ["RESUMED", "AUTOSTART"]


async def test_automatic_start_waits_for_the_mandated_holdings(launched):
    launched._autostart_step(["Mandatory holding missing: BA 195C"])
    assert launched.mode == "PAUSED" and launched.autostart_pending
    assert "waits for the mandated holdings" in launched.health
    assert launched.execution.paused and launched.broker.readonly


async def test_automatic_start_keeps_every_paper_activation_check(launched):
    launched._autostart_step([])
    launched.broker.qualified = False  # e.g. IBKR contract identities not verified yet
    await launched._resume_after_data_wait()
    assert launched.mode == "WAITING_DATA" and launched.execution.paused
    assert "Confirm contest configuration" in launched.health


@pytest.mark.parametrize("action", ["pause", "shadow"])
async def test_operator_controls_cancel_a_pending_automatic_start(launched, action):
    await launched.control(action)
    assert not launched.autostart_pending
    launched._autostart_step([])
    assert launched.mode in ("PAUSED", "SHADOW") and launched.data_wait is None


async def test_dashboard_switch_persists_and_off_cancels_a_pending_start(manifest, store, launched):
    launched._autostart_step([])
    state = launched.set_autostart("off")
    assert state["autostart"]["mode"] == "off" and state["autostart"]["source"] == "dashboard"
    assert launched.mode == "PAUSED" and "cancelled by operator" in launched.health
    restarted = TradingService(autostarting(manifest), store, demo=True)
    restarted.restore_autostart()
    assert restarted.autostart_mode() == "off" and not restarted.autostart_pending
    # A configuration without autostart can be switched on from the dashboard.
    plain = TradingService(manifest, store, demo=True)
    plain.restore_autostart()
    assert plain.autostart_mode() == "off"
    assert plain.set_autostart("paper")["autostart"]["pending"]
    with pytest.raises(ValueError):
        plain.set_autostart("shadow")


def test_autostart_endpoint_needs_the_control_token(manifest, store):
    from fastapi.testclient import TestClient

    from hedge.webapp import create_app

    service = TradingService(manifest, store, demo=True)
    client = TestClient(create_app(service))  # no lifespan: nothing connects or trades
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    assert client.post("/api/autostart", json={"mode": "paper"}).status_code == 403
    response = client.post("/api/autostart", json={"mode": "paper"}, headers={"X-Control-Token": token})
    assert response.status_code == 200 and response.json()["autostart"]["mode"] == "paper"
    bad = client.post("/api/autostart", json={"mode": "shadow"}, headers={"X-Control-Token": token})
    assert bad.status_code == 422


async def test_repeated_alerts_are_not_flooded_and_record_times_carry_utc(launched):
    for _ in range(3):
        launched.alert("error", "ValueError: Required holding/order contracts unavailable: 4762")
        launched.alert("error", "disconnected")
    alerts = launched.store.records("alert", launched.manifest.account)
    assert [a["message"] for a in alerts].count("disconnected") == 1
    assert all(a["at"].endswith("+00:00") for a in alerts)

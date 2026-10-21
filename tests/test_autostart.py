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
    assert "contracts are not verified" in launched.health


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


async def test_contest_setup_is_manual_and_confirmation_can_come_from_the_dashboard(store):
    from hedge.demo import demo_manifest

    contest = demo_manifest().model_copy(update={"confirmed": False})
    service = TradingService(contest, store, demo=True)
    assert not service.contest_confirmed()
    with pytest.raises(ValueError, match="configuration changed"):
        service.confirm_configuration("stale-hash")
    state = service.confirm_configuration(contest.config_hash)
    assert state["confirmed"] and service.contest_confirmed()
    record = store.records("configuration_confirmation", contest.account)[0]
    assert record["config_hash"] == contest.config_hash and record["mandatory"]
    # Any configuration change needs a fresh confirmation.
    changed = TradingService(
        contest.model_copy(update={"initial_nav": contest.initial_nav + 1}), store, demo=True
    )
    assert changed.manifest.config_hash != contest.config_hash and not changed.contest_confirmed()


async def test_seven_leg_contest_refuses_automatic_purchases(manifest, store):
    contest = manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})
    service = TradingService(contest, store, demo=True)
    service.autostart_pending, epoch = True, service.control_epoch
    for action in ("arm_setup", "setup"):
        with pytest.raises(ValueError, match="enter the seven legs in TWS"):
            await service.control(action)
    assert service.autostart_pending and service.control_epoch == epoch  # nothing disturbed


async def test_readiness_checklist_names_what_blocks_paper(launched):
    items = {i["label"]: i for i in launched.checklist()}
    assert items["Mandated legs"]["status"] == "ok"
    assert items["Automatic paper start"]["detail"].startswith("On")
    assert items["Trading"]["status"] == "warn"
    launched.broker.positions.clear()
    launched.last_snapshot = launched.broker.snapshot()
    legs = {i["label"]: i for i in launched.checklist()}["Mandated legs"]
    assert legs["status"] == "fail" and "Enter in TWS (Paper): BUY" in legs["detail"]
    state = launched.state()
    assert state["checklist"] and state["mandatory_legs"] and state["config_hash"]
    # Leftover positions: the IBKR row names the real cause instead of "open TWS".
    launched.last_monitor_error = (
        "Positions not in the contest configuration (close them in TWS): "
        "BA stock -1141 [4762]; ITA stock +200 [39046959]"
    )
    rows = {i["label"]: i for i in launched.checklist()}
    assert "non-contest positions" in rows["IBKR"]["detail"]
    assert rows["Account holdings"]["detail"].endswith("BA stock -1141 [4762]; ITA stock +200 [39046959]")
    assert rows["Automatic paper start"]["status"] == "warn"
    assert "rows above" in rows["Trading"]["detail"]  # the cause is stated once, not on every row


def test_leftover_positions_are_named_as_tws_shows_them(manifest):
    import asyncio
    from types import SimpleNamespace

    from hedge.broker import IBKRBroker

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.native = {c.con_id: object() for c in manifest.contracts}
    broker.ib.positions = lambda account: [
        SimpleNamespace(contract=SimpleNamespace(conId=4762, secType="STK", symbol="BA"), position=-1141.0),
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=920159842,
                secType="OPT",
                symbol="BA",
                lastTradeDateOrContractMonth="20261023",
                strike=195.0,
                right="C",
            ),
            position=-1.0,
        ),
    ]
    snapshot = SimpleNamespace(quantities=lambda: {4762: -1141, 920159842: -1}, orders=())
    with pytest.raises(ValueError) as error:
        broker._validate_required_qualification(snapshot)
    assert str(error.value) == (
        "Positions not in the contest configuration (close them in TWS): "
        "BA stock -1141 [4762]; BA 20261023 195C -1 [920159842]"
    )


def test_close_import_accepts_the_statement_figure_as_printed():
    from hedge.webapp import CloseImport

    row = CloseImport(date="2026-10-21", nav="$1,002,345.67", source="IBKR statement", account="DU1")
    assert str(row.nav) == "1002345.67"


async def test_collector_codes_and_daily_history_refresh_need_no_terminal(
    manifest, store, tmp_path, monkeypatch
):
    import json
    from datetime import datetime

    from hedge.config import NY
    from hedge.moomoo_data import collector_codes

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    contest = manifest.model_copy(
        update={"market_data_provider": "moomoo", "daily_stock_history": "data/daily.csv"}
    )
    service = TradingService(contest, store, demo=True)
    service.demo = False  # exercise the live-only paths without a broker
    service.now = lambda: datetime(2026, 10, 22, 10, 0, tzinfo=NY)
    codes = tmp_path / "config/local.moomoo-codes.json"
    codes.parent.mkdir()
    codes.write_text('["US.OLD"]')
    service._write_collector_codes()
    assert json.loads(codes.read_text()) == collector_codes(contest)
    assert (tmp_path / "config/local.moomoo-codes.json.bak").read_text() == '["US.OLD"]'

    fake = tmp_path / ".runtime/moomoo-env/bin/python"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\necho 'Archived 12 prior completed daily observations'\n")
    fake.chmod(0o755)
    status = await service.refresh_daily_history("test")
    assert status["status"] == "ok" and status["as_of"] == "2026-10-22"
    assert "Archived 12" in status["message"]
    service.now = lambda: datetime(2026, 10, 22, 15, 50, tzinfo=NY)
    with pytest.raises(ValueError, match="closing window"):
        service.start_history_refresh("button")


def test_record_times_are_utc_from_either_database():
    from datetime import datetime

    from hedge.config import NY
    from hedge.storage import utc_iso

    assert utc_iso(datetime(2026, 10, 21, 6, 11, 35)) == "2026-10-21T06:11:35+00:00"  # SQLite (naive)
    assert utc_iso(datetime(2026, 10, 21, 2, 11, 35, tzinfo=NY)) == "2026-10-21T06:11:35+00:00"  # PostgreSQL

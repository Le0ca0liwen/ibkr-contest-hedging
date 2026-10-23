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


async def test_quotes_row_counts_every_contract_and_fails_on_a_failed_subscription(launched):
    from types import SimpleNamespace

    total = len(launched.manifest.contracts)
    assert {i["label"]: i for i in launched.checklist()}["Quotes"]["status"] == "ok"
    dropped = next(iter(launched.last_quotes))
    launched.last_quotes = {i: q for i, q in launched.last_quotes.items() if i != dropped}
    row = {i["label"]: i for i in launched.checklist()}["Quotes"]
    assert row["status"] == "warn"
    assert row["detail"] == f"{total - 1} of {total} quotes live; no quote at all for 1"
    launched.moomoo_data = SimpleNamespace(subscription_failed=["US.BA"])
    row = {i["label"]: i for i in launched.checklist()}["Quotes"]
    assert row["status"] == "fail" and "Moomoo subscription failed for US.BA" in row["detail"]


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


async def test_refused_enable_paper_keeps_the_automatic_start_it_interrupted(launched):
    launched._autostart_step([])
    launched.broker.qualified = False
    with pytest.raises(ValueError, match="contracts are not verified"):
        await launched.control("paper")
    wait = launched.data_wait
    assert launched.mode == "WAITING_DATA" and wait["autostart"] and wait["epoch"] == launched.control_epoch
    launched.broker.qualified = True
    assert risk_now(launched).passed
    await launched._resume_after_data_wait()
    assert launched.mode == "PAPER"


async def test_refused_enable_paper_keeps_a_pending_automatic_start_armed(launched):
    launched._autostart_step(["Mandatory holding missing: BA 195C"])
    launched.broker.qualified = False
    with pytest.raises(ValueError):
        await launched.control("paper")
    assert launched.mode == "PAUSED" and launched.autostart_pending


async def test_a_refused_operator_action_never_leaves_an_orphaned_wait(launched, monkeypatch):
    launched._autostart_step([])
    monkeypatch.setattr(launched.store, "intents", lambda *args, **kwargs: [{"state": "CANCEL_PENDING"}])
    with pytest.raises(ValueError, match="pending cancellations"):
        await launched.control("shadow")
    assert launched.mode == "PAUSED" and launched.data_wait is None and launched.pause_reason
    assert launched.execution.paused and launched.broker.readonly


async def test_auto_paper_off_stops_an_activation_already_in_progress(launched):
    launched._autostart_step([])
    assert risk_now(launched).passed
    entered, release = asyncio.Event(), asyncio.Event()
    reconcile = launched.reconcile

    async def slow_reconcile():
        entered.set()
        await release.wait()
        return await reconcile()

    launched.reconcile = slow_reconcile
    activation = asyncio.create_task(launched._resume_after_data_wait())
    await entered.wait()  # the automatic activation holds the control lock now
    launched.set_autostart("off")
    release.set()
    await activation
    assert launched.mode == "PAUSED" and launched.execution.paused and launched.broker.readonly


async def test_a_failing_automatic_start_is_not_rearmed_every_loop(launched, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("hedge.service.time.monotonic", lambda: clock[0])
    launched._autostart_step([])
    launched._hard_stop("ValueError: something unexpected")
    launched.autostart_pending = True  # what the monitor does after an unexpected error
    launched._autostart_step([])
    assert launched.mode == "PAUSED" and "unexpected" in launched.health  # the cause stays visible
    clock[0] += 15
    launched._autostart_step([])
    assert launched.mode == "WAITING_DATA"
    statuses = [r["status"] for r in launched.store.records("continuity", launched.manifest.account)]
    assert statuses.count("AUTOSTART") == 1  # retries are not logged every loop


async def test_auto_paper_switch_answers_while_an_activation_holds_the_control_lock(launched):
    import httpx

    from hedge.webapp import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(launched)), base_url="http://testserver"
    ) as client:
        token = re.search(r'name="control-token" content="([^"]+)"', (await client.get("/")).text).group(1)
        launched._autostart_step([])
        async with launched.control_lock:  # e.g. the automatic activation awaiting IBKR
            response = await asyncio.wait_for(
                client.post("/api/autostart", json={"mode": "off"}, headers={"X-Control-Token": token}), 5
            )
            assert response.status_code == 200 and launched.mode == "PAUSED"


def test_a_mistyped_close_is_refused_without_a_snapshot_and_a_bad_one_can_be_corrected(
    manifest, store, tmp_path
):
    from datetime import datetime

    from fastapi.testclient import TestClient

    from hedge.config import NY
    from hedge.webapp import create_app

    service = TradingService(manifest, store, demo=True)  # never connected: no snapshot
    service.now = lambda: datetime(2026, 10, 7, 8, tzinfo=NY)
    client = TestClient(create_app(service))
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    nav = manifest.initial_nav

    def post(value):
        return client.post(
            "/api/closes",
            json={"date": "2026-10-06", "nav": str(value), "source": "IBKR statement", "account": "DU_TEST"},
            headers={"X-Control-Token": token},
        )

    assert post(nav / 10).status_code == 422  # a missing digit
    service.ledger.import_close(manifest.day_zero, nav / 10, "earlier typo", manifest.account)
    assert post(nav + 2345).status_code == 200  # the correction is not judged against the typo
    bad = tmp_path / "closes.csv"
    bad.write_text("date,nav,source,account\n2026-10-06,0,statement,DU_TEST\n")
    with pytest.raises(ValueError, match="Invalid NAV"):
        service.ledger.import_csv(bad)


async def test_unknown_holdings_never_produce_buy_sell_instructions(launched):
    launched.last_snapshot = None
    legs = {i["label"]: i for i in launched.checklist()}["Mandated legs"]
    assert legs["status"] == "warn" and "BUY" not in legs["detail"] and "do not enter" in legs["detail"]
    launched.last_snapshot = launched.broker.snapshot().model_copy(update={"connected": False})
    legs = {i["label"]: i for i in launched.checklist()}["Mandated legs"]
    assert legs["status"] == "warn" and "last known: 2 of 2 held" in legs["detail"]


async def test_the_automatic_start_row_tells_whether_it_is_armed(launched):
    def row():
        return {i["label"]: i for i in launched.checklist()}["Automatic paper start"]

    assert row()["status"] == "ok" and row()["detail"].startswith("On")
    await launched.control("shadow")
    assert row()["status"] == "warn" and "On, but cancelled" in row()["detail"]
    trading = {i["label"]: i for i in launched.checklist()}["Trading"]
    assert "Enable paper trading" in trading["detail"] or "rows above" not in trading["detail"]
    # Turning the switch off and on again arms it from shadow too.
    launched.set_autostart("off")
    launched.set_autostart("paper")
    assert launched.autostart_pending
    launched._autostart_step([])
    assert launched.mode == "WAITING_DATA" and launched.data_wait["autostart"]


def test_a_refused_control_names_the_reconciliation_cause_not_a_tws_outage(manifest, store):
    from fastapi.testclient import TestClient

    from hedge.webapp import create_app

    service = TradingService(manifest, store, demo=True)  # not connected

    async def unreachable():
        raise ConnectionError("Not connected")

    service.reconcile = unreachable
    client = TestClient(create_app(service))
    token = re.search(r'name="control-token" content="([^"]+)"', client.get("/").text).group(1)
    headers = {"X-Control-Token": token}
    assert client.post("/api/control", json={"action": "reconcile"}, headers=headers).status_code == 503
    service.last_monitor_error = (
        "Positions not in the contest configuration (close them in TWS): BA stock -1141 [4762]"
    )
    response = client.post("/api/control", json={"action": "reconcile"}, headers=headers)
    assert response.status_code == 409 and "TWS answers" in response.json()["detail"]
    assert "BA stock -1141" in response.json()["detail"]


async def test_auto_paper_off_during_a_refused_enable_does_not_bring_the_automatic_start_back(launched):
    launched._autostart_step([])
    launched.broker.qualified = False
    reconcile = launched.reconcile

    async def switch_off_meanwhile():
        launched.set_autostart("off")
        return await reconcile()

    launched.broker.qualified = True
    launched.reconcile = switch_off_meanwhile
    launched.last_scenarios = None  # the Enable is refused after its reconcile
    with pytest.raises(ValueError):
        await launched.control("paper")
    assert launched.mode == "PAUSED" and launched.data_wait is None and not launched.autostart_pending


async def test_paper_that_runs_no_longer_carries_a_pending_automatic_start(launched):
    launched.set_autostart("paper")  # pressed while PAUSED, just before Enable
    assert launched.autostart_pending
    assert risk_now(launched).passed
    await launched.control("paper")
    assert launched.mode == "PAPER" and not launched.autostart_pending


async def test_a_refused_enable_from_shadow_keeps_the_automatic_start_armed(launched):
    await launched.control("shadow")
    launched.set_autostart("off")
    launched.set_autostart("paper")
    launched.broker.qualified = False
    with pytest.raises(ValueError):
        await launched.control("paper")
    assert launched.mode == "SHADOW" and launched.autostart_pending


async def test_a_refused_shadow_while_paper_runs_shows_the_stop(launched, monkeypatch):
    launched._autostart_step([])
    assert risk_now(launched).passed
    await launched._resume_after_data_wait()
    assert launched.mode == "PAPER"
    monkeypatch.setattr(launched.store, "intents", lambda *args, **kwargs: [{"state": "CANCEL_PENDING"}])
    with pytest.raises(ValueError, match="pending cancellations"):
        await launched.control("shadow")
    assert launched.mode == "PAUSED" and "Shadow requested" in launched.health
    assert launched.execution.paused and launched.broker.readonly


def test_the_statement_nav_is_the_official_close_in_the_contest(manifest, store):
    """The instructor scores the IBKR statement NAV (Oct 23): an API capture is provisional, and
    the app asks for the statement before the next session's open."""
    from datetime import datetime

    from hedge.config import NY

    contest = manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})
    service = TradingService(contest, store, demo=True)
    assert not service.api_close_official and not service.closing_recorder.api_official
    assert service.closing_recorder.end_of_day  # the predicted statement NAV is still recorded at 20:05
    day = contest.scoring_sessions[0]
    service.now = lambda: datetime.combine(day, datetime.min.time(), NY).replace(hour=10)
    service.ledger.import_close(
        contest.day_zero, contest.initial_nav, "IBKR API closing observation", contest.account
    )
    record = service.ledger.previous_close_record(day)
    assert record["provenance"] == "api"
    service._warn_api_previous_close(day, record)
    assert any("statement" in a["message"] for a in store.records("alert", contest.account))
    row = {i["label"]: i for i in service.checklist()}["Previous official close"]
    assert row["status"] == "warn" and "statement" in row["detail"]  # a warning: paper still starts


async def test_a_changed_configuration_with_the_same_account_and_legs_is_confirmed_automatically(store):
    """Operator instruction (Oct 23): no Confirm press for a settings change."""
    from hedge.demo import demo_manifest

    contest = demo_manifest().model_copy(update={"confirmed": False})
    first = TradingService(contest, store, demo=True)
    assert not first.auto_confirm()  # no earlier confirmation and no reconciled holdings yet
    first.confirm_configuration(contest.config_hash)
    changed = TradingService(
        contest.model_copy(update={"initial_nav": contest.initial_nav + 1}), store, demo=True
    )
    assert not changed.contest_confirmed()
    assert changed.auto_confirm() and changed.contest_confirmed()
    record = next(
        r
        for r in store.records("configuration_confirmation", contest.account)
        if r["config_hash"] == changed.manifest.config_hash
    )
    assert record["automatic"] and record["basis"] == contest.config_hash
    assert any("confirmed automatically" in a["message"] for a in store.records("alert", contest.account))
    assert not changed.auto_confirm()  # already confirmed: once
    # Different mandated legs: not from the earlier confirmation, only from IBKR's holdings.
    legs = dict(contest.mandatory)
    first_leg = next(iter(legs))
    legs[first_leg] += 1
    other = TradingService(
        contest.model_copy(update={"mandatory": legs, "initial_nav": contest.initial_nav + 2}),
        store,
        demo=True,
    )
    assert not other.auto_confirm() and not other.contest_confirmed()
    assert other.auto_confirm(holdings_match=True) and other.contest_confirmed()


async def test_the_configuration_confirms_itself_once_ibkr_holds_the_configured_legs(
    manifest, store, quotes, now, monkeypatch
):
    """Operator instruction (Oct 23): never a Confirm press. Without an earlier confirmation, the
    monitor confirms the configuration on the first IBKR reconcile whose holdings equal the
    configured mandated legs, and only then may paper start."""
    contest = autostarting(manifest).model_copy(update={"confirmed": False})
    service = TradingService(contest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    service.broker.update_quotes(fresh, now)
    service.ledger.import_close(manifest.day_zero, manifest.initial_nav, "fixture", manifest.account)
    service.restore_autostart()
    row = {i["label"]: i for i in service.checklist()}["Contest configuration"]
    assert row["status"] == "fail" and "confirms by itself" in row["detail"]
    legs = dict(service.broker.positions)
    missing = next(iter(contest.mandatory))
    service.broker.positions[missing] = 0  # one mandated leg not entered yet

    async def no_optimizer():
        pass

    monkeypatch.setattr(service, "optimize", no_optimizer)
    task = asyncio.create_task(service._monitor_loop())
    try:
        for _ in range(50):
            await asyncio.sleep(0.05)
            if service.last_snapshot is not None:
                break
        await asyncio.sleep(0.2)
        assert not service.contest_confirmed()  # holdings differ from the configured legs
        service.broker.positions.update(legs)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if service.contest_confirmed():
                break
        assert service.contest_confirmed()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    record = store.records("configuration_confirmation", contest.account)[0]
    assert record["automatic"] and record["basis"] == "ibkr_holdings"
    assert record["mandatory"] == service._confirmed_legs()
    messages = [a["message"] for a in store.records("alert", contest.account)]
    assert any("holds exactly the" in m and "confirmed automatically" in m for m in messages)
    assert {i["label"]: i for i in service.checklist()}["Contest configuration"]["status"] == "ok"


async def test_paper_is_armed_again_when_each_sessions_trading_starts(launched):
    """Operator instruction (Oct 23): with Auto paper ON a pause or safety stop lasts until the next
    session's trading start at most; with Auto paper OFF it stays paused."""
    from datetime import datetime, timedelta

    from hedge.config import NY

    service = launched
    day = service.manifest.scoring_sessions[0]
    clock = [datetime.combine(day, datetime.min.time(), NY).replace(hour=3)]
    service.now = lambda: clock[0]
    service._hard_stop("Operator requested pause")
    service.autostart_pending = False
    service._arm_at_trading_start()
    assert not service.autostart_pending  # no trading window yet
    clock[0] = clock[0].replace(hour=11)  # trading has started
    service._arm_at_trading_start()
    assert service.autostart_pending and service.autostart_session == day
    statuses = [r["status"] for r in service.store.records("continuity", service.manifest.account)]
    assert statuses[0] == "AUTOSTART_ARMED"
    service.autostart_pending = False  # e.g. the operator paused again later that day
    clock[0] = clock[0].replace(hour=14)
    service._arm_at_trading_start()
    assert not service.autostart_pending  # once per session
    following = service.manifest.scoring_sessions[1]
    clock[0] = datetime.combine(following, datetime.min.time(), NY).replace(hour=11)
    service._arm_at_trading_start()
    assert service.autostart_pending  # the next session's trading start arms it again
    service.set_autostart("off")
    service.autostart_session = None
    clock[0] += timedelta(days=1)
    service._arm_at_trading_start()
    assert not service.autostart_pending  # Auto paper OFF: stays paused

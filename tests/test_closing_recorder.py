import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hedge.broker import IBKRBroker
from hedge.closing import ClosingRecorder
from hedge.config import NY, Manifest
from hedge.scoring import ScoreLedger


class NavBroker:
    def __init__(self, manifest, clock):
        self.manifest, self.clock = manifest, clock
        self.calls = 0
        self.fail = False
        self.changes = {}

    async def closing_nav_observation(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("disconnected")
        return {
            "account": self.manifest.account,
            "nav": Decimal("1000100.12"),
            "requested_at": self.clock(),
            "observed_at": self.clock(),
            "source": "ibkr",
            **self.changes,
        }


def prepared(store, manifest):
    ledger = ScoreLedger(store, manifest)
    ledger.import_close(manifest.day_zero, manifest.initial_nav, "IBKR statement", manifest.account)
    time = [manifest.session_close(manifest.scoring_sessions[0])]
    broker = NavBroker(manifest, lambda: time[0])
    alerts = []
    recorder = ClosingRecorder(manifest, ledger, broker, lambda: time[0], lambda *x: alerts.append(x))
    return ledger, time, broker, recorder, alerts


async def test_close_capture_resets_next_baseline_once_and_preserves_statement_revisions(store, manifest):
    ledger, clock, broker, recorder, _ = prepared(store, manifest)
    await recorder.tick()
    day = manifest.scoring_sessions[0]
    assert ledger.closes()[day] == Decimal("1000100.12")
    assert ledger.previous_close(manifest.scoring_sessions[1]) == Decimal("1000100.12")
    assert "API closing observation" in ledger.latest_report()["source"]
    clock[0] += timedelta(seconds=20)
    await recorder.tick()
    restarted = ClosingRecorder(manifest, ledger, broker, lambda: clock[0], lambda *x: None)
    await restarted.tick()
    assert broker.calls == 1
    ledger.import_close(day, Decimal("1000090.12"), "Corrected IBKR statement", manifest.account)
    await recorder.tick()
    assert ledger.previous_close(manifest.scoring_sessions[1]) == Decimal("1000090.12")
    assert ledger.latest_report()["source"] == "Corrected IBKR statement"


async def test_exchange_early_close_and_weekends(store, manifest):
    m = Manifest(
        name="Calendar fixture",
        account=manifest.account,
        day_zero=date(2026, 11, 25),
        scoring_sessions=(date(2026, 11, 27), date(2026, 11, 30)),
        contracts=(manifest.contract_map[10],),
        mandatory={10: 1},
        allowlist=(),
        valuation_convention="fixture",
        report_source="fixture",
    )
    _, clock, broker, recorder, _ = prepared(store, m)
    clock[0] = datetime(2026, 11, 27, 12, 59, 59, tzinfo=NY)
    await recorder.tick()
    assert broker.calls == 0
    clock[0] += timedelta(seconds=1)
    await recorder.tick()
    assert broker.calls == 1
    clock[0] = datetime(2026, 11, 28, 16, tzinfo=NY)
    await recorder.tick()
    assert broker.calls == 1


async def test_disconnect_retries_within_close_window(store, manifest):
    ledger, clock, broker, recorder, _ = prepared(store, manifest)
    broker.fail = True
    await recorder.tick()
    assert manifest.scoring_sessions[0] not in ledger.closes()
    clock[0] += timedelta(seconds=5)
    broker.fail = False
    await recorder.tick()
    assert broker.calls == 1
    clock[0] += timedelta(seconds=10)
    await recorder.tick()
    assert broker.calls == 2
    assert manifest.scoring_sessions[0] in ledger.closes()


@pytest.mark.parametrize("invalid", ["cached", "wrong_account", "wrong_source", "nonfinite", "late"])
async def test_invalid_observations_do_not_create_a_close(store, manifest, invalid):
    ledger, clock, broker, recorder, _ = prepared(store, manifest)
    if invalid == "cached":
        broker.changes = {"requested_at": clock[0] - timedelta(seconds=1)}
    elif invalid == "wrong_account":
        broker.changes = {"account": "DU_OTHER"}
    elif invalid == "wrong_source":
        broker.changes = {"source": "moomoo"}
    elif invalid == "nonfinite":
        broker.changes = {"nav": Decimal("NaN")}
    else:
        broker.changes = {"observed_at": clock[0] + timedelta(seconds=181)}
    await recorder.tick()
    assert manifest.scoring_sessions[0] not in ledger.closes()


async def test_missed_window_is_durable_and_never_backfilled_with_current_nav(store, manifest):
    ledger, clock, broker, recorder, alerts = prepared(store, manifest)
    clock[0] += timedelta(seconds=181)
    await recorder.tick()
    await ClosingRecorder(manifest, ledger, broker, lambda: clock[0], lambda *x: alerts.append(x)).tick()
    assert broker.calls == 0
    assert manifest.scoring_sessions[0] not in ledger.closes()
    assert len(alerts) == 1
    assert len(store.records("closing_capture", manifest.account)) == 1


async def test_report_arriving_during_request_wins(store, manifest):
    ledger, _, broker, recorder, _ = prepared(store, manifest)
    original = broker.closing_nav_observation

    async def reply():
        ledger.import_close(
            manifest.scoring_sessions[0], Decimal("1000250"), "IBKR statement", manifest.account
        )
        return await original()

    broker.closing_nav_observation = reply
    await recorder.tick()
    assert ledger.closes()[manifest.scoring_sessions[0]] == Decimal("1000250")
    assert ledger.latest_report()["source"] == "IBKR statement"


async def test_post_close_capture_supersedes_bell_once_and_yields_to_statement(store, manifest):
    ledger, clock, broker, recorder, alerts = prepared(store, manifest)
    day, following = manifest.scoring_sessions[:2]
    close = clock[0]
    await recorder.tick()
    broker.changes = {"nav": Decimal("1000640.55")}  # SPY options marked at 16:15; account refreshed
    clock[0] = close + timedelta(minutes=19, seconds=59)
    await recorder.tick()
    assert broker.calls == 1
    clock[0] = close + timedelta(minutes=20)
    await recorder.tick()
    assert broker.calls == 2 and ledger.closes()[day] == Decimal("1000640.55")
    record = ledger.previous_close_record(following)
    assert record["provenance"] == "api"
    assert record["source"].startswith("IBKR API post-close observation (after 16:15 options close")
    assert ledger.summary()["days"][0]["provenance"] == "api"
    assert recorder.state()["last"]["phase"] == "post_close"
    clock[0] += timedelta(minutes=1)
    await recorder.tick()
    await ClosingRecorder(manifest, ledger, broker, lambda: clock[0], lambda *x: None).tick()
    assert broker.calls == 2  # once per session, also across restarts
    assert sorted(r["phase"] for r in store.records("closing_capture", manifest.account)) == [
        "close",
        "post_close",
    ]
    ledger.import_close(day, Decimal("1000312.40"), "IBKR Custom Statement NAV", manifest.account)
    ledger.import_close(day, Decimal("1000999"), "IBKR API closing observation (re-run)", manifest.account)
    assert ledger.previous_close(following) == Decimal("1000312.40")
    assert ledger.previous_close_record(following)["provenance"] == "statement"
    assert ledger.latest_report()["provenance"] == "statement"
    assert not alerts


@pytest.mark.parametrize("case", ["statement", "missed_bell", "disabled", "stale", "window_passed"])
async def test_post_close_capture_only_follows_an_unreconciled_bell_capture(store, manifest, case):
    from hedge.closing import POST_CLOSE
    from hedge.config import ClosingCapturePolicy

    if case == "disabled":
        manifest = manifest.model_copy(
            update={"closing_capture": ClosingCapturePolicy(late_capture_minutes=0)}
        )
    ledger, clock, broker, recorder, _ = prepared(store, manifest)
    day, close = manifest.scoring_sessions[0], clock[0]
    if case != "missed_bell":
        await recorder.tick()
    if case == "statement":
        ledger.import_close(day, Decimal("1000400"), "IBKR Custom Statement NAV", manifest.account)
    if case == "stale":  # a value requested before the post-close time is not a fresh observation
        broker.changes = {"requested_at": close + timedelta(minutes=19)}
    clock[0] = close + timedelta(minutes=25, seconds=1 if case == "window_passed" else 0)
    await recorder.tick()
    assert broker.calls == {"missed_bell": 0, "stale": 2}.get(case, 1)
    assert not any(POST_CLOSE in r["source"] for r in ledger.revisions())
    if case == "missed_bell":  # never backfilled; the statement remains required
        assert day not in ledger.closes()
        assert store.records("closing_capture", manifest.account)[0]["status"] == "MISSED"
    if case == "statement":
        assert ledger.closes()[day] == Decimal("1000400")


async def test_statement_arriving_during_post_close_request_wins(store, manifest):
    ledger, clock, broker, recorder, _ = prepared(store, manifest)
    day = manifest.scoring_sessions[0]
    await recorder.tick()
    original = broker.closing_nav_observation

    async def reply():
        ledger.import_close(day, Decimal("1000250"), "IBKR Custom Statement NAV", manifest.account)
        return await original()

    broker.closing_nav_observation = reply
    clock[0] += timedelta(minutes=20)
    await recorder.tick()
    assert broker.calls == 2 and ledger.closes()[day] == Decimal("1000250")
    assert len([r for r in ledger.revisions() if r["day"] == day]) == 2
    assert "post-close observation discarded" in recorder.status


def test_api_previous_close_is_exposed_and_warned_once_per_session(store, manifest, snapshot, quotes):
    from hedge.service import TradingService
    from hedge.supervisor import Supervisor

    service = TradingService(manifest, store, demo=True)
    service.last_snapshot, service.last_quotes = snapshot, quotes
    assert manifest.active_session(service.now()) == manifest.scoring_sessions[0]
    source = "IBKR API post-close observation (after 16:15 options close; NetLiquidation)"
    service.ledger.import_close(manifest.day_zero, Decimal("1000640.55"), source, manifest.account)

    def warnings():
        return [
            a
            for a in store.records("alert", manifest.account)
            if a["message"].startswith("Import yesterday's official IBKR statement NAV before the open")
        ]

    state = service.state()
    assert state["previous_close_provenance"] == "api"
    assert Decimal(state["previous_close"]) == Decimal("1000640.55")
    assert Supervisor.context(state)["previous_close_provenance"] == "api"
    service.state()
    TradingService(manifest, store, demo=True).state()  # a restart cannot repeat the warning
    assert len(warnings()) == 1 and warnings()[0]["severity"] == "warning"
    assert str(manifest.day_zero) in warnings()[0]["message"]
    service.ledger.import_close(
        manifest.day_zero, Decimal("1000312.40"), "IBKR Custom Statement NAV", manifest.account
    )
    state = service.state()
    assert state["previous_close_provenance"] == "statement"
    assert Decimal(state["previous_close"]) == Decimal("1000312.40")
    assert len(warnings()) == 1


@pytest.mark.parametrize("provide_fresh_value", [False, True])
async def test_broker_summary_capture_requires_fresh_scoped_event(manifest, provide_fresh_value):
    import asyncio

    from eventkit import Event

    broker = IBKRBroker(manifest, asyncio.Queue())
    event = Event()
    future = None
    cancelled = []

    def start(request_id):
        nonlocal future
        future = asyncio.get_running_loop().create_future()
        return future

    def end(request_id):
        if not future.done():
            future.set_result(None)

    def request(request_id, group, tags):
        assert tags == "NetLiquidation"
        event.emit(SimpleNamespace(account="DU_OTHER", tag=tags, value="99", currency="USD"))
        if provide_fresh_value:
            event.emit(
                SimpleNamespace(account=manifest.account, tag=tags, value="1000123.45", currency="USD")
            )
        end(request_id)

    broker.ib = SimpleNamespace(
        isConnected=lambda: True,
        managedAccounts=lambda: [manifest.account],
        accountSummaryEvent=event,
        accountSummary=lambda: [SimpleNamespace(value="999999")],  # cache must never be consulted
        wrapper=SimpleNamespace(startReq=start, _endReq=end),
        client=SimpleNamespace(
            getReqId=lambda: 42,
            reqAccountSummary=request,
            cancelAccountSummary=lambda i: cancelled.append(i),
        ),
    )
    if provide_fresh_value:
        result = await broker.closing_nav_observation()
        assert result["nav"] == Decimal("1000123.45")
        assert result["requested_at"] <= result["observed_at"]
    else:
        with pytest.raises(ValueError, match="Fresh IBKR closing NAV unavailable"):
            await broker.closing_nav_observation()
    assert cancelled == [42] and len(event) == 0


async def test_failed_qualification_disconnects_and_cannot_leave_partial_readiness(manifest):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())
    disconnected = []
    broker.ib = SimpleNamespace(disconnect=lambda: disconnected.append(True))
    broker.qualified = broker.reconciled = True

    async def fail():
        raise ConnectionError("qualification failure")

    broker._connect_and_qualify = fail
    with pytest.raises(ConnectionError):
        await broker.connect()
    assert disconnected and not broker.qualified and not broker.reconciled


async def test_initial_connection_downloads_cannot_overlap_manual_reconciliation(manifest):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())
    started, release = asyncio.Event(), asyncio.Event()
    downloads = []

    async def download():
        downloads.append(True)
        return "snapshot"

    async def connect_and_qualify():
        started.set()
        await release.wait()
        await download()

    broker._connect_and_qualify = connect_and_qualify
    broker._download_state = download
    initial = asyncio.create_task(broker.connect())
    await started.wait()
    manual = asyncio.create_task(broker.reconcile())
    await asyncio.sleep(0.01)
    assert not downloads
    release.set()
    await asyncio.gather(initial, manual)
    assert len(downloads) == 2


async def test_qualification_timeout_is_bounded_and_disconnects(manifest, monkeypatch):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())
    disconnected = []

    async def connected(*args, **kwargs):
        return None

    broker.ib = SimpleNamespace(
        connectAsync=connected,
        managedAccounts=lambda: [manifest.account],
        reqContractDetailsAsync=lambda native: asyncio.get_running_loop().create_future(),
        disconnect=lambda: disconnected.append(True),
    )
    original_wait = asyncio.wait_for

    async def bounded(future, timeout):
        assert timeout == 15
        return await original_wait(future, 0.01)

    monkeypatch.setattr("hedge.broker.asyncio.wait_for", bounded)
    with pytest.raises(ConnectionError, match="Contract qualification timed out: BA"):
        await broker.connect()
    assert disconnected and not broker.qualified and not broker.reconciled


@pytest.mark.parametrize("exposure", ["unheld", "held", "working"])
async def test_optional_timeout_only_quarantines_unheld_unordered_contract(manifest, snapshot, exposure):
    import asyncio

    from hedge.models import Position, WorkingOrder

    m = manifest.model_copy(update={"market_data_provider": "moomoo"})
    broker = IBKRBroker(m, asyncio.Queue())

    async def connected(*args, **kwargs):
        return None

    broker.ib = SimpleNamespace(connectAsync=connected, managedAccounts=lambda: [m.account])

    async def qualify(c):
        if c.con_id == 103:
            raise TimeoutError
        broker.native[c.con_id] = object()

    broker._qualify_contract = qualify
    if exposure == "held":
        snapshot = snapshot.model_copy(
            update={"positions": (*snapshot.positions, Position(con_id=103, quantity=1))}
        )
    if exposure == "working":
        snapshot = snapshot.model_copy(
            update={"orders": (WorkingOrder(order_id=1, con_id=103, signed_remaining=1),)}
        )

    async def download():
        return snapshot

    broker._download_state = download
    if exposure == "unheld":
        await broker._connect_and_qualify()
        assert broker.qualified and 103 not in broker.native
        assert 103 in broker.qualification_errors
    else:
        with pytest.raises(ValueError, match="Contest contracts not verified by IBKR"):
            await broker._connect_and_qualify()
        assert not broker.qualified


def test_external_quotes_for_unqualified_candidates_are_excluded(manifest, quotes):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.native = {10: object(), 101: object(), 102: object()}
    broker.external_quotes = lambda now: quotes
    assert set(broker.quotes()) == {10, 101, 102}


async def test_native_contract_id_mismatch_is_rejected(manifest):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())

    async def details(c):
        return [
            SimpleNamespace(contract=SimpleNamespace(conId=999, symbol="BA", secType="STK", currency="USD"))
        ]

    broker.ib = SimpleNamespace(reqContractDetailsAsync=details)
    with pytest.raises(ValueError, match="Contract identity mismatch"):
        await broker._qualify_contract(manifest.contract_map[10])
    assert not broker.native


async def test_submit_rejects_unqualified_contract_even_with_ready_account(manifest):
    import asyncio

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker.ib = SimpleNamespace(isConnected=lambda: True)
    broker.readonly = False
    broker.qualified = broker.reconciled = True
    with pytest.raises(ValueError, match="Order contract has not passed broker qualification"):
        await broker.submit(SimpleNamespace(con_id=10), 1)


def test_close_marks_keep_last_pre_close_capture_and_one_post_close(store, manifest):
    from hedge.closing import CloseMarkRecorder, post_close_window

    store.initialize()
    day = manifest.scoring_sessions[0]
    close = manifest.session_close(day)
    time = [close - timedelta(seconds=150)]
    calls = []

    def collect(now, session, phase):
        calls.append((now, phase))
        return {"date": str(session), "phase": phase, "at": now.isoformat()}

    recorder = CloseMarkRecorder(manifest, store, lambda: time[0], collect, lambda *x: None)
    recorder.tick()
    assert not calls  # outside the final two minutes
    for offset in (100, 30, 1):
        time[0] = close - timedelta(seconds=offset)
        recorder.tick()
    time[0] = close + timedelta(seconds=1)
    recorder.tick()
    rows = store.records("close_marks", manifest.account)
    assert [r["phase"] for r in rows] == ["pre_close"]
    assert rows[0]["at"] == (close - timedelta(seconds=1)).isoformat()  # last capture before the bell
    # Post-close marks wait for the post-close NAV observation (16:20-16:25 by default): IBKR's marks
    # then include SPY options' 16:15 close, as the official post-close NAV does.
    start, end = post_close_window(manifest, day)
    assert (start, end) == (close + timedelta(minutes=20), close + timedelta(minutes=25))
    for moment in (
        close + timedelta(seconds=40),
        start - timedelta(seconds=1),
        start,
        start + timedelta(minutes=1),
    ):
        time[0] = moment
        recorder.tick()
    rows = store.records("close_marks", manifest.account)
    assert sorted(r["phase"] for r in rows) == ["post_close", "pre_close"]
    assert next(r for r in rows if r["phase"] == "post_close")["at"] == start.isoformat()
    # A restarted recorder cannot duplicate the session's captures.
    again = CloseMarkRecorder(manifest, store, lambda: time[0], collect, lambda *x: None)
    again.tick()
    assert len(store.records("close_marks", manifest.account)) == 2


@pytest.mark.asyncio
async def test_service_close_marks_include_quotes_greeks_and_risk(tmp_path, monkeypatch):
    from hedge.demo import demo_manifest
    from hedge.service import TradingService
    from hedge.storage import Store

    monkeypatch.chdir(tmp_path)
    service = TradingService(demo_manifest(), Store(f"sqlite:///{tmp_path}/marks.db"), demo=True)
    await service.start()
    try:
        await asyncio.sleep(0.2)
        now = service.now()
        day = service.manifest.active_session(now) or service.manifest.day_zero
        payload = service._close_marks(now, day, "pre_close")
        assert payload["quotes"] and payload["positions"]
        sample = next(iter(payload["quotes"].values()))
        assert {"bid", "ask", "mid", "iv", "delta", "gamma", "vega", "theta", "data_type"} <= sample.keys()
        assert payload["ibkr_marks"] == {}  # simulator has no IBKR marks
        store = service.store
        store.record("close_marks", service.manifest.account, payload)
        assert store.records("close_marks", service.manifest.account, 1)
    finally:
        await service.stop()


def test_close_marks_wait_for_data_instead_of_recording_an_empty_capture(store, manifest):
    from hedge.closing import CloseMarkRecorder, post_close_window

    store.initialize()
    day = manifest.scoring_sessions[0]
    start, end = post_close_window(manifest, day)
    time = [start + timedelta(seconds=40)]  # service (re)started inside the post-close window
    ready = [False]

    def collect(now, session, phase):
        return {"phase": phase, "at": now.isoformat()} if ready[0] else None

    recorder = CloseMarkRecorder(manifest, store, lambda: time[0], collect, lambda *x: None)
    recorder.tick()
    assert not store.records("close_marks", manifest.account)
    ready[0], time[0] = True, start + timedelta(seconds=55)
    recorder.tick()
    rows = store.records("close_marks", manifest.account)
    assert [r["at"] for r in rows] == [(start + timedelta(seconds=55)).isoformat()]
    time[0] = end + timedelta(seconds=1)  # one capture per session
    recorder.tick()
    assert len(store.records("close_marks", manifest.account)) == 1


def test_post_close_marks_follow_the_bell_when_late_capture_is_off(store, manifest):
    from hedge.closing import CloseMarkRecorder, post_close_window

    store.initialize()
    m = manifest.model_copy(
        update={"closing_capture": manifest.closing_capture.model_copy(update={"late_capture_minutes": 0})}
    )
    day = m.scoring_sessions[0]
    close = m.session_close(day)
    assert post_close_window(m, day) == (close + timedelta(seconds=30), close + timedelta(seconds=300))
    time = [close + timedelta(seconds=20)]
    recorder = CloseMarkRecorder(
        m, store, lambda: time[0], lambda now, d, phase: {"at": now.isoformat()}, print
    )
    recorder.tick()
    assert not store.records("close_marks", m.account)
    time[0] = close + timedelta(seconds=45)
    recorder.tick()
    assert [r["at"] for r in store.records("close_marks", m.account)] == [time[0].isoformat()]


async def test_post_close_reading_far_from_the_bell_keeps_the_bell_value(store, manifest):
    ledger, clock, broker, recorder, alerts = prepared(store, manifest)
    day = manifest.scoring_sessions[0]
    close = clock[0]
    await recorder.tick()
    bell = ledger.closes()[day]
    # Extended-hours stock marks moved NAV $5,000 by 16:20: not a better close than the bell.
    broker.changes = {"nav": bell + Decimal("5000")}
    clock[0] = close + timedelta(minutes=20)
    await recorder.tick()
    assert ledger.closes()[day] == bell
    statuses = [r["status"] for r in store.records("closing_capture", manifest.account)]
    assert "DIVERGED" in statuses
    assert any("diverged" in str(alert) for alert in alerts)
    calls = broker.calls
    clock[0] += timedelta(minutes=1)
    await recorder.tick()
    assert broker.calls == calls  # no retry after a divergence


def contest_prepared(store, manifest):
    contest = manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})
    return contest, *prepared(store, contest)


async def test_contest_missed_bell_is_retried_once_after_the_options_close(store, manifest):
    contest, ledger, clock, broker, recorder, alerts = contest_prepared(store, manifest)
    day, close = contest.scoring_sessions[0], clock[0]
    store.record(
        "snapshot",
        contest.account,
        {"at": (close - timedelta(seconds=40)).isoformat(), "nav": "1000050.00"},
    )
    broker.fail = True
    await recorder.tick()
    clock[0] = close + timedelta(minutes=3, seconds=1)
    await recorder.tick()
    assert "retrying after the options close (16:20 ET)" in recorder.status
    assert not store.records("closing_capture", contest.account)
    assert all("waiting for retry" in a[1] for a in alerts)
    broker.fail = False
    clock[0] = close + timedelta(minutes=20, seconds=5)
    await recorder.tick()
    assert ledger.closes()[day] == Decimal("1000100.12")
    source = ledger.latest_report()["source"]
    assert (
        "post-close observation" in source and "official close" in source and "bell capture missed" in source
    )
    assert recorder.state()["last"]["phase"] == "post_close"
    calls = broker.calls
    clock[0] += timedelta(minutes=1)
    await recorder.tick()
    await ClosingRecorder(contest, ledger, broker, lambda: clock[0], lambda *x: None).tick()
    assert broker.calls == calls and all("waiting for retry" in a[1] for a in alerts)


async def test_contest_missed_bell_and_post_close_require_a_manual_close(store, manifest):
    contest, ledger, clock, broker, recorder, alerts = contest_prepared(store, manifest)
    day, close = contest.scoring_sessions[0], clock[0]
    broker.fail = True
    clock[0] = close + timedelta(minutes=20)
    await recorder.tick()
    assert day not in ledger.closes() and not store.records("closing_capture", contest.account)
    clock[0] = close + timedelta(minutes=25, seconds=1)
    await recorder.tick()
    assert day not in ledger.closes()
    assert store.records("closing_capture", contest.account)[0]["status"] == "MISSED"
    assert any("enter IBKR's closing NAV" in a[1] for a in alerts)


async def test_contest_post_close_reading_far_from_the_pre_close_nav_is_not_used(store, manifest):
    contest, ledger, clock, broker, recorder, alerts = contest_prepared(store, manifest)
    day, close = contest.scoring_sessions[0], clock[0]
    store.record(
        "snapshot", contest.account, {"at": (close - timedelta(seconds=30)).isoformat(), "nav": "998000"}
    )
    store.record(  # after the close: never the reference
        "snapshot", contest.account, {"at": (close + timedelta(minutes=5)).isoformat(), "nav": "1000100"}
    )
    clock[0] = close + timedelta(minutes=20)
    await recorder.tick()
    assert day not in ledger.closes()
    row = store.records("closing_capture", contest.account)[0]
    assert row["status"] == "MISSED" and row["pre_close_nav"] == "998000"
    assert "differed from the last pre-close NAV" in alerts[0][1]
    clock[0] += timedelta(seconds=30)
    await recorder.tick()
    await ClosingRecorder(contest, ledger, broker, lambda: clock[0], lambda *x: None).tick()
    assert broker.calls == 1 and day not in ledger.closes()


async def test_a_hung_observation_cannot_stall_the_recorder(store, manifest, monkeypatch):
    import hedge.closing as closing

    monkeypatch.setattr(closing, "OBSERVE_TIMEOUT_SECONDS", 0.05)
    ledger, clock, broker, recorder, alerts = prepared(store, manifest)

    async def hang():
        await asyncio.sleep(3600)

    broker.closing_nav_observation = hang
    await asyncio.wait_for(recorder.tick(), 1)
    assert "waiting for retry" in recorder.status and alerts


async def test_closing_observation_does_not_wait_for_a_reconnect(manifest):
    from eventkit import Event

    broker = IBKRBroker(manifest, asyncio.Queue())
    event = Event()
    futures = {}

    def start(request_id):
        futures[request_id] = asyncio.get_running_loop().create_future()
        return futures[request_id]

    def request(request_id, group, tags):
        event.emit(SimpleNamespace(account=manifest.account, tag=tags, value="1000123.45", currency="USD"))
        futures[request_id].set_result(None)

    broker.ib = SimpleNamespace(
        isConnected=lambda: True,
        managedAccounts=lambda: [manifest.account],
        accountSummaryEvent=event,
        wrapper=SimpleNamespace(startReq=start, _endReq=lambda i: None),
        client=SimpleNamespace(
            getReqId=lambda: 7, reqAccountSummary=request, cancelAccountSummary=lambda i: None
        ),
    )
    async with broker.reconcile_lock:  # e.g. connect() re-qualifying every contract
        result = await asyncio.wait_for(broker.closing_nav_observation(), 1)
    assert result["nav"] == Decimal("1000123.45")

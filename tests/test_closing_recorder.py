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
        with pytest.raises(ValueError, match="Required holding/order contracts unavailable"):
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
    from hedge.closing import CloseMarkRecorder

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
    for offset in (40, 60):
        time[0] = close + timedelta(seconds=offset)
        recorder.tick()
    assert sorted(r["phase"] for r in store.records("close_marks", manifest.account)) == [
        "post_close",
        "pre_close",
    ]
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

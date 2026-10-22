import fcntl
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from hedge.service import TradingService

ROOT = Path(__file__).resolve().parents[1]


def test_a_second_quote_collector_names_the_holder_and_exits_with_its_own_code(tmp_path):
    (tmp_path / "data/moomoo").mkdir(parents=True)
    (tmp_path / "codes.json").write_text('["US.SPY"]')
    with (tmp_path / "data/moomoo/collector.lock").open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.write("4242")
        held.flush()
        result = subprocess.run(
            [sys.executable, str(ROOT / "ops/record_moomoo.py"), "--codes", "codes.json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 75 and "pid 4242" in result.stdout


async def test_collector_supervision_survives_failures_and_names_a_leftover_collector(
    manifest, store, monkeypatch, tmp_path
):
    import asyncio

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/moomoo").mkdir(parents=True)
    (tmp_path / "data/moomoo/collector.lock").write_text("4242")
    service = TradingService(manifest, store, demo=True)
    service.market_data_process = SimpleNamespace(returncode=75)
    starts = []

    async def start():
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("database went away")  # must not end supervision
        service.market_data_process = SimpleNamespace(returncode=None)

    service._start_collector = start
    real_sleep = asyncio.sleep
    monkeypatch.setattr("hedge.service.asyncio.sleep", lambda seconds: real_sleep(0))
    loop = asyncio.create_task(service._collector_loop())
    for _ in range(200):
        await real_sleep(0)
        if len(starts) >= 2:
            break
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    messages = [a["message"] for a in store.records("alert", manifest.account)]
    assert len(starts) == 2
    assert any("pid 4242" in m and "Activity Monitor" in m for m in messages)
    assert any("database went away" in m for m in messages)


def test_launcher_history_step_never_blocks_or_crashes_the_launch(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "ops"))
    from ops import run_contest

    run_contest.step("Slow optional step", [sys.executable, "-c", "import time; time.sleep(5)"], False, 0.5)
    run_contest.step("Missing optional program", ["/nonexistent/python"], required=False)
    out = capsys.readouterr().out
    assert out.count("Warning:") == 2 and "TimeoutExpired" in out and "FileNotFoundError" in out
    assert not run_contest.port_in_use(1)  # nothing listens on a privileged port in tests


def test_collector_retries_a_failed_subscription_until_it_succeeds(monkeypatch):
    import threading

    monkeypatch.syspath_prepend(str(ROOT / "ops"))
    from ops import record_moomoo

    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_json(self, orient):
            import json

            return json.dumps(self.rows)

    sdk = SimpleNamespace(RET_OK=0, SubType=SimpleNamespace(QUOTE="Q", ORDER_BOOK="B", K_1M="K"))
    failing = {"US.SPCX"}
    calls = []

    def subscribe(codes, types, is_first_push, subscribe_push):
        calls.append((tuple(codes), tuple(types)))
        return (1, "busy") if set(codes) & failing else (0, "")

    ctx = SimpleNamespace(
        subscribe=subscribe,
        get_stock_quote=lambda codes: (0, Frame([{"code": codes[0]}])),
        get_order_book=lambda code, num: (0, {"code": code, "Bid": [[1, 1]], "Ask": [[1.1, 1]]}),
    )
    lock, securities = threading.Lock(), {}
    failed = [c for c in ("US.SPCX", "US.RKLB") if not record_moomoo.subscribe(sdk, ctx, lock, securities, c)]
    assert failed == ["US.SPCX"] and securities["US.SPCX"]["error"] == record_moomoo.SUBSCRIPTION_FAILED
    assert "error" not in securities["US.RKLB"]
    assert record_moomoo.retry_failed(sdk, ctx, lock, securities, failed) == ["US.SPCX"]
    failing.clear()
    assert record_moomoo.retry_failed(sdk, ctx, lock, securities, failed) == []
    assert "error" not in securities["US.SPCX"] and securities["US.SPCX"]["order_book"]["code"] == "US.SPCX"
    assert calls[-1] == (("US.SPCX",), ("K",))  # the recovered stock gets its 1-minute bars again

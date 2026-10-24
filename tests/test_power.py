"""macOS power and sleep risk: pmset and ioreg parsing, failure paths, risk levels, caching."""

import subprocess
from datetime import datetime, timedelta

import pytest

from hedge.config import NY
from hedge.power import IOREG, PMSET, PowerMonitor, parse_pmset, power_status, sleep_risk

BATTERY = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=21758051)\t55%; discharging; 3:12 remaining present: true\n"
)
CHARGED = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=21758051)\t100%; charged; 0:00 remaining present: true\n"
)
DESKTOP = "Now drawing from 'AC Power'\n"


def lid(state):
    return f'+-o IOPMrootDomain  <class IOPMrootDomain>\n  {{\n    "AppleClamshellState" = {state}\n  }}\n'


def fake_run(pmset, ioreg="", pmset_code=0, ioreg_code=0, calls=None):
    def run(args, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "timeout": 5}
        if calls is not None:
            calls.append(args)
        if args == PMSET:
            return subprocess.CompletedProcess(args, pmset_code, pmset, "")
        if args == IOREG:
            return subprocess.CompletedProcess(args, ioreg_code, ioreg, "")
        raise AssertionError(args)

    return run


def test_battery_with_the_lid_open():
    status = power_status(fake_run(BATTERY, lid("No")), "darwin")
    assert status == {
        "source": "battery",
        "percent": 55,
        "lid_closed": False,
        "detail": "Battery Power, 55%; discharging; 3:12 remaining, lid open",
    }


def test_charged_laptop_with_the_lid_closed():
    status = power_status(fake_run(CHARGED, lid("Yes")), "darwin")
    assert status["source"] == "ac"
    assert status["percent"] == 100
    assert status["lid_closed"] is True
    assert status["detail"] == "AC Power, 100%; charged; 0:00 remaining, lid closed"


def test_desktop_has_no_battery_or_lid():
    status = power_status(fake_run(DESKTOP, ""), "darwin")
    assert status == {"source": "ac", "percent": None, "lid_closed": None, "detail": "AC Power"}


@pytest.mark.parametrize(
    "text, parsed",
    [
        (
            "Currently drawing from 'Battery Power'\n -InternalBattery-0\t62%; discharging; 2:56 remaining\n",
            ("battery", 62, "Battery Power, 62%; discharging; 2:56 remaining"),
        ),
        (
            "Now drawing from 'Battery Power'\r\n -InternalBattery-0 (id=1)\t9%; discharging; (no estimate)"
            " present: true\r\n",
            ("battery", 9, "Battery Power, 9%; discharging; (no estimate)"),
        ),
        (
            "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging"
            " present: true\n",
            ("ac", 80, "AC Power, 80%; AC attached; not charging"),
        ),
        (
            "Now drawing from 'UPS Power'\n -CP1500PFCLCD (id=2)\t95%; discharging; 0:45 remaining present: true\n",
            ("battery", 95, "UPS Power, 95%; discharging; 0:45 remaining"),
        ),
        ("", ("unknown", None, "No power source reported")),
    ],
)
def test_pmset_variants(text, parsed):
    assert parse_pmset(text) == parsed


def test_default_run_is_subprocess_run(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run(BATTERY, lid("Yes")))
    status = power_status(platform="darwin")
    assert (status["source"], status["percent"], status["lid_closed"]) == ("battery", 55, True)


def test_ioreg_failure_keeps_the_power_source():
    status = power_status(fake_run(BATTERY, ioreg_code=1), "darwin")
    assert (status["source"], status["percent"], status["lid_closed"]) == ("battery", 55, None)
    assert "lid unknown (ioreg exited with code 1)" in status["detail"]


@pytest.mark.parametrize("error", [FileNotFoundError, subprocess.TimeoutExpired(PMSET, 5), PermissionError])
def test_a_command_that_raises_gives_unknown(error):
    def run(args, **kwargs):
        raise error

    status = power_status(run, "darwin")
    assert status["source"] == "unknown"
    assert status["percent"] is None and status["lid_closed"] is None
    assert status["detail"].startswith("pmset failed:")


def test_nonzero_exit_gives_unknown():
    status = power_status(fake_run("garbage", pmset_code=1), "darwin")
    assert status == {
        "source": "unknown",
        "percent": None,
        "lid_closed": None,
        "detail": "pmset exited with code 1",
    }


def test_unrecognized_output_gives_unknown_source():
    status = power_status(fake_run("", ""), "darwin")
    assert status["source"] == "unknown"
    assert sleep_risk(status, 30) == ("ok", "Power status unknown")


def test_other_platforms_never_run_commands():
    calls = []
    status = power_status(fake_run(BATTERY, lid("Yes"), calls=calls), "linux")
    assert calls == []
    assert status["source"] == "unknown"
    assert status["lid_closed"] is None
    assert "linux" in status["detail"]


def given(source="ac", percent=None, lid=False):
    return {"source": source, "percent": percent, "lid_closed": lid, "detail": ""}


def test_ac_with_the_lid_open_is_ok():
    assert sleep_risk(given("ac", 100, False), 10) == ("ok", "On AC power with the lid open")
    assert sleep_risk(given("ac", None, None), 10) == ("ok", "On AC power")


def test_unknown_is_ok():
    assert sleep_risk(given("unknown", None, None), 10) == ("ok", "Power status unknown")
    assert sleep_risk(PowerMonitor().status, 10) == ("ok", "Power status unknown")


def test_battery_warns_with_its_percent():
    level, message = sleep_risk(given("battery", 55, False), 30)
    assert level == "warn"
    assert message == (
        "On battery (55%): plug in the Mac and keep the lid open through 20:10 ET; "
        "a sleeping Mac misses the 16:00 closing capture"
    )


@pytest.mark.parametrize(
    "percent, minutes, level",
    [
        (19, 60, "fail"),
        (19, 0, "fail"),
        (19, 30.5, "fail"),
        (19, 61, "warn"),
        (19, -1, "warn"),
        (19, None, "warn"),
        (20, 30, "warn"),
        (None, 30, "warn"),
    ],
)
def test_low_battery_fails_only_near_the_close(percent, minutes, level):
    assert sleep_risk(given("battery", percent, False), minutes)[0] == level


@pytest.mark.parametrize("minutes, level", [(0, "fail"), (60, "fail"), (60.01, "warn"), (-0.5, "warn")])
def test_closed_lid_fails_near_the_close(minutes, level):
    result, message = sleep_risk(given("ac", 100, True), minutes)
    assert result == level
    assert "Lid closed: open the lid" in message


def test_battery_and_closed_lid_together():
    level, message = sleep_risk(given("battery", 80, True), 45)
    assert level == "fail"
    assert message.startswith("Close in 45 min. On battery (80%) with the lid closed: plug in the Mac")


def test_monitor_refreshes_at_most_every_interval():
    calls = []
    monitor = PowerMonitor(fake_run(BATTERY, lid("No"), calls=calls), "darwin", interval=60)
    start = datetime(2026, 10, 23, 15, 40, tzinfo=NY)
    first = monitor.check(start)
    assert first["source"] == "battery" and len(calls) == 2
    assert monitor.check(start + timedelta(seconds=59)) is first
    assert len(calls) == 2
    monitor.check(start + timedelta(seconds=60))
    assert len(calls) == 4
    monitor.check(start + timedelta(seconds=30))  # clock went back: refresh
    assert len(calls) == 6


async def test_the_service_warns_before_the_close_on_battery_but_never_blocks_paper(manifest, store):
    from datetime import datetime

    from hedge.config import NY
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    day = manifest.scoring_sessions[0]
    service.power.check = lambda now: {"source": "battery", "percent": 55, "lid_closed": False, "detail": ""}
    await service._power_step(datetime.combine(day, datetime.min.time(), NY).replace(hour=12))
    assert not store.records("alert", manifest.account)  # midday: the readiness row only
    row = {i["label"]: i for i in service.checklist()}["Power"]
    assert row["status"] == "warn" and "plug in the Mac" in row["detail"]
    await service._power_step(datetime.combine(day, datetime.min.time(), NY).replace(hour=15, minute=10))
    assert any("On battery (55%)" in a["message"] for a in store.records("alert", manifest.account))
    service.power.check = lambda now: {"source": "battery", "percent": 12, "lid_closed": False, "detail": ""}
    await service._power_step(datetime.combine(day, datetime.min.time(), NY).replace(hour=15, minute=30))
    assert service.power_risk[0] == "fail"
    assert {i["label"]: i for i in service.checklist()}["Power"]["status"] == "warn"

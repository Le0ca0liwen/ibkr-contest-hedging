"""macOS power and sleep risk: warn before the close when the Mac may sleep.

On Oct 23 a laptop on battery slept from 15:45 ET and the closing capture was lost. pmset reports
the power source and charge, ioreg the lid (clamshell) state. Anything else is "unknown"; nothing
here raises.
"""

import re
import subprocess
import sys

PMSET = ["pmset", "-g", "batt"]
IOREG = ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"]
TIMEOUT = 5  # seconds per command
NEAR_CLOSE = 60  # minutes
LOW_BATTERY = 20  # percent
CONSEQUENCE = "a sleeping Mac misses the 16:00 closing capture"


def command_output(run, args):
    """(stdout, None) for a command that succeeded, else (None, why it did not)."""
    try:
        result = run(args, capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode != 0:
            return None, f"{args[0]} exited with code {result.returncode}"
        return str(result.stdout or ""), None
    except Exception as exc:
        return None, f"{args[0]} failed: {type(exc).__name__}"


def parse_pmset(text):
    """(source "ac"/"battery"/"unknown", percent or None, detail) from `pmset -g batt` output."""
    drawing = re.search(r"drawing from '([^']+)'", text)
    name = drawing.group(1) if drawing else ""
    source = (
        "ac" if name.startswith("AC") else "battery" if name.startswith(("Battery", "UPS")) else "unknown"
    )
    battery = re.search(r"\b(\d{1,3})%;([^\n]*)", text)
    if battery is None:
        return source, None, name or "No power source reported"
    state = re.sub(r"\s*present:.*$", "", battery.group(2)).strip()
    return source, int(battery.group(1)), f"{name}, {battery.group(1)}%; {state}".strip(", ;")


def parse_clamshell(text):
    """True for a closed lid, False for an open one, None without a lid (desktop Macs)."""
    match = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)\b', text)
    return None if match is None else match.group(1) == "Yes"


def power_status(run=None, platform=None):
    """{"source", "percent", "lid_closed", "detail"} for this Mac; source "unknown" off macOS or on
    a pmset failure. An ioreg failure leaves only lid_closed None."""
    run = run or subprocess.run
    platform = platform or sys.platform
    unknown = {"source": "unknown", "percent": None, "lid_closed": None}
    if platform != "darwin":
        return {**unknown, "detail": f"Power status needs macOS (this is {platform})"}
    text, error = command_output(run, PMSET)
    if error:
        return {**unknown, "detail": error}
    source, percent, detail = parse_pmset(text)
    text, error = command_output(run, IOREG)
    lid = None if error else parse_clamshell(text)
    lid_detail = f"lid unknown ({error})" if error else {True: "lid closed", False: "lid open"}.get(lid)
    detail = ", ".join(part for part in (detail, lid_detail) if part)
    return {"source": source, "percent": percent, "lid_closed": lid, "detail": detail}


def sleep_risk(status, minutes_to_close):
    """(level "ok"/"warn"/"fail", operator message). Battery or a closed lid warns; a battery under
    20% or a closed lid fails within 60 minutes before the close (0 <= minutes_to_close <= 60). A
    missing status (PowerMonitor.status before its first check) is unknown."""
    status = status or {}
    source, percent = status.get("source"), status.get("percent")
    battery, lid = source == "battery", status.get("lid_closed") is True
    if not battery and not lid:
        if source == "ac":
            return "ok", "On AC power" + (" with the lid open" if status.get("lid_closed") is False else "")
        return "ok", "Power status unknown"
    charge = "" if percent is None else f" ({percent}%)"
    if battery and lid:
        action = f"On battery{charge} with the lid closed: plug in the Mac and open the lid"
    elif battery:
        action = f"On battery{charge}: plug in the Mac and keep the lid open"
    else:
        action = "Lid closed: open the lid and keep it open"
    message = f"{action} through 20:10 ET; {CONSEQUENCE}"
    near = minutes_to_close is not None and 0 <= minutes_to_close <= NEAR_CLOSE
    if near and (lid or (battery and percent is not None and percent < LOW_BATTERY)):
        return "fail", f"Close in {minutes_to_close:.0f} min. {message}"
    return "warn", message


class PowerMonitor:
    """power_status, refreshed at most every `interval` seconds."""

    def __init__(self, run=None, platform=None, interval=60):
        self.run, self.platform, self.interval = run, platform, interval
        self.status, self.checked = None, None

    def check(self, now):
        """The cached status; refreshed first when `interval` seconds have passed since `now` last did
        (or the clock went back)."""
        if self.checked is None or not 0 <= (now - self.checked).total_seconds() < self.interval:
            self.status, self.checked = power_status(self.run, self.platform), now
        return self.status

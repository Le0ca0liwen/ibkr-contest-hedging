"""Read-only probe: can Moomoo or IBKR supply October's option closing data for tuning the rebuild?

    .runtime/moomoo-env/bin/python ops/probe_close_sources.py moomoo [--start 2026-10-01 --end 2026-10-23]
    .venv/bin/python ops/probe_close_sources.py ibkr --config config/local.contest.yaml [--day 2026-10-22]
    .venv/bin/python ops/probe_close_sources.py spy --config config/local.contest.yaml [--days 2026-10-21 ...]
    .venv/bin/python ops/probe_close_sources.py ibkr-daily --config config/local.contest.yaml

moomoo: daily K-lines (quote-only) of the option codes already in this month's historical quota, so
no quota is spent (--allow-new N spends at most N), with each bar's close and last_close next to the
IBKR statement's Close Price (data/calibration/statements). Every bar's last_close equal to the
previous day's statement price would mean Moomoo's daily history carries the clearing house's price
of every listed option on every day: the truth for tuning.
ibkr: one read-only API connection (the app's client id + 300) asks for 1-second bid/ask bars of the
held options around 16:00 on a past day: bars would mean IBKR can supply October's closing books.
spy: SPY's share price around the close on each --days day (one-minute BID, ASK and MIDPOINT bars
15:55-16:20, one-second MIDPOINT bars 15:59:30-16:00:30): OCC's SPY prices imply a share price
0.08-0.49 away from the 16:00 midpoint, and the minute it matches says when OCC's SPY input is taken.
ibkr-daily: IBKR's daily bars (TRADES, MIDPOINT, BID, ASK) of every held option still listed, over the
last month, each close next to that day's statement price: a series that equals it would give past
OCC prices for every listed contract, not just held ones.
Writes data/calibration/probe-<source>.json. Never trades.
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATEMENTS = ROOT / "data/calibration/statements"
CLIENT_OFFSET = 300  # the app uses its client id and +100; the overnight marks script used 98
CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")


def moomoo_code(symbol, expiry, right, strike):
    return f"US.{symbol}{expiry.replace('-', '')[2:]}{right}{round(float(strike) * 1000)}"


def statement_prices(folder=STATEMENTS):
    """{day: {Moomoo code: statement Close Price}} for every option on a saved statement."""
    prices = {}
    for path in sorted(Path(folder).glob("*.json")):
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") == "OPT" and p.get("mark") not in (None, ""):
                code = moomoo_code(p["symbol"].split()[0], p["expiry"], p["put_call"], p["strike"])
                prices.setdefault(data["date"], {})[code] = float(p["mark"])
    return prices


def compare(bars, prices):
    """Rows of (code, day, close, last_close, statement that day, statement the day before) and the
    count of last_close values equal to the previous statement price (to 1e-4)."""
    days = sorted(prices)
    rows, matches, checked = [], 0, 0
    for code, items in sorted(bars.items()):
        for bar in items:
            day = bar["time_key"][:10]
            before = [d for d in days if d < day]
            previous = prices[before[-1]].get(code) if before else None
            rows.append((code, day, bar["close"], bar["last_close"], prices.get(day, {}).get(code), previous))
            if previous is not None and bar["last_close"] is not None:
                checked += 1
                matches += abs(bar["last_close"] - previous) < 1e-4
    return rows, matches, checked


def probe_moomoo(args):
    import moomoo as sdk

    prices = statement_prices()
    held = sorted({code for day in prices.values() for code in day})
    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, quota = ctx.get_history_kl_quota(get_detail=True)
        used = {row.get("code") for row in (quota[2] if ret == sdk.RET_OK else [])}
        codes = [c for c in held if c in used] + [c for c in held if c not in used][: args.allow_new]
        bars, errors = {}, []
        for code in codes:
            ret, data, _ = ctx.request_history_kline(
                code, start=str(args.start), end=str(args.end), ktype=sdk.KLType.K_DAY, max_count=100
            )
            if ret != sdk.RET_OK:
                errors.append(f"{code}: {data}")
                continue
            bars[code] = [
                {
                    k: (None if row[k] != row[k] else row[k])
                    for k in ("time_key", "close", "last_close", "volume")
                }
                for _, row in data.iterrows()
            ]
    finally:
        ctx.close()
    rows, matches, checked = compare(bars, prices)
    for row in rows:
        print("{:24} {} close {} last_close {} | statement {} / day before {}".format(*row))
    skipped = [c for c in held if c not in codes]
    print(
        f"{matches} of {checked} last_close values equal the previous statement price; "
        f"{len(bars)} codes, {len(skipped)} skipped to save quota, {len(errors)} errors"
    )
    return {"bars": bars, "matches": matches, "checked": checked, "skipped": skipped, "errors": errors}


def probe_ibkr(args):
    import sys

    import yaml
    from ib_async import IB, Option
    from ib_async.ib import StartupFetchNONE

    sys.path.insert(0, str(ROOT))
    from hedge.broker import without_summary_resubscribe

    config = yaml.safe_load(Path(args.config).read_text())
    host, port = config.get("gateway_host", "127.0.0.1"), int(config["gateway_port"])
    client = int(config.get("client_id", 1)) + CLIENT_OFFSET
    held = sorted({code for day in statement_prices().values() for code in day})
    contracts = []
    for code in held:
        symbol, ymd, right, strike = CODE.match(code).groups()
        expiry, strike = "20" + ymd, int(strike) / 1000
        if expiry >= f"{args.day:%Y%m%d}":  # expired options have no IBKR history
            contracts.append((code, Option(symbol, expiry, strike, right, "SMART", currency="USD")))
    ib, errors = without_summary_resubscribe(IB()), []
    ib.errorEvent += lambda req, code, message, contract: errors.append(f"{code}: {message}")
    ib.connect(host, port, clientId=client, readonly=True, timeout=15, fetchFields=StartupFetchNONE)
    result = {}
    try:
        end = f"{args.day:%Y%m%d} 16:00:30 US/Eastern"
        for code, contract in contracts[: args.limit]:
            if not ib.qualifyContracts(contract):
                result[code] = {"error": "not qualified"}
                continue
            found = {}
            for what in ("BID_ASK", "MIDPOINT"):
                before = len(errors)
                bars = ib.reqHistoricalData(contract, end, "60 S", "1 secs", what, useRTH=False, formatDate=2)
                found[what] = {
                    "bars": [
                        {"at": str(b.date), "open": b.open, "high": b.high, "low": b.low, "close": b.close}
                        for b in bars[-5:]
                    ],
                    "count": len(bars),
                    "errors": errors[before:],
                }
                print(code, what, len(bars), "bars", (bars[-1] if bars else errors[before:]))
            result[code] = found
            ib.sleep(2)  # well inside IBKR's historical pacing (60 requests per 10 minutes)
    finally:
        ib.disconnect()
    worked = sum(1 for r in result.values() if r.get("BID_ASK", {}).get("count"))
    print(f"BID_ASK bars for {worked} of {len(result)} options on {args.day}")
    return {"day": str(args.day), "options": result, "errors": errors}


def probe_spy(args):
    import sys

    import yaml
    from ib_async import IB, Stock
    from ib_async.ib import StartupFetchNONE

    sys.path.insert(0, str(ROOT))
    from hedge.broker import without_summary_resubscribe

    config = yaml.safe_load(Path(args.config).read_text())
    ib, errors = without_summary_resubscribe(IB()), []
    ib.errorEvent += lambda req, code, message, contract: errors.append(f"{code}: {message}")
    client = int(config.get("client_id", 1)) + CLIENT_OFFSET + 1  # beside a running books fetch
    ib.connect(
        config.get("gateway_host", "127.0.0.1"),
        int(config["gateway_port"]),
        clientId=client,
        readonly=True,
        timeout=15,
        fetchFields=StartupFetchNONE,
    )
    days = {}
    try:
        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)
        for day in args.days:
            found = {}
            for what, end, duration, size in (
                ("BID", "16:20:00", "1500 S", "1 min"),
                ("ASK", "16:20:00", "1500 S", "1 min"),
                ("MIDPOINT", "16:20:00", "1500 S", "1 min"),
                ("MIDPOINT", "16:00:30", "60 S", "1 secs"),
            ):
                bars = ib.reqHistoricalData(
                    spy, f"{day:%Y%m%d} {end} US/Eastern", duration, size, what, useRTH=False, formatDate=2
                )
                key = f"{what} {size}"
                found[key] = [{"at": str(b.date), "close": b.close} for b in bars]
                print(day, key, len(bars), "bars", bars[-1].close if bars else errors[-1:])
                ib.sleep(1)
            days[str(day)] = found
    finally:
        ib.disconnect()
    return {"days": days, "errors": errors}


def daily_matches(bars, prices):
    """{what: (closes equal to the statement price to 1e-4, closes compared)} for
    bars = {code: {what: [{"day", "close"}]}} and prices = {day: {code: statement price}}."""
    found = {}
    for code, kinds in bars.items():
        for what, items in kinds.items():
            equal, compared = found.get(what, (0, 0))
            for bar in items:
                price = prices.get(bar["day"], {}).get(code)
                if price is not None and bar["close"] is not None:
                    equal, compared = equal + (abs(bar["close"] - price) < 1e-4), compared + 1
            found[what] = (equal, compared)
    return found


def probe_ibkr_daily(args):
    import sys

    import yaml
    from ib_async import IB, Option
    from ib_async.ib import StartupFetchNONE

    sys.path.insert(0, str(ROOT))
    from hedge.broker import without_summary_resubscribe

    config = yaml.safe_load(Path(args.config).read_text())
    prices = statement_prices()
    today = f"{date.today():%Y%m%d}"
    contracts = []
    for code in sorted({code for day in prices.values() for code in day}):
        symbol, ymd, right, strike = CODE.match(code).groups()
        if "20" + ymd >= today:  # expired options have no IBKR history
            contracts.append(
                (code, Option(symbol, "20" + ymd, int(strike) / 1000, right, "SMART", currency="USD"))
            )
    ib, errors = without_summary_resubscribe(IB()), []
    ib.errorEvent += lambda req, code, message, contract: errors.append(f"{code}: {message}")
    ib.connect(
        config.get("gateway_host", "127.0.0.1"),
        int(config["gateway_port"]),
        clientId=int(config.get("client_id", 1)) + CLIENT_OFFSET + 3,  # beside the books fetches
        readonly=True,
        timeout=15,
        fetchFields=StartupFetchNONE,
    )
    bars = {}
    try:
        for code, contract in contracts[: args.limit]:
            if not ib.qualifyContracts(contract):
                continue
            for what in ("TRADES", "MIDPOINT", "BID", "ASK"):
                rows = ib.reqHistoricalData(contract, "", "1 M", "1 day", what, useRTH=True, formatDate=1)
                bars.setdefault(code, {})[what] = [{"day": str(b.date)[:10], "close": b.close} for b in rows]
                ib.sleep(1)
    finally:
        ib.disconnect()
    found = daily_matches(bars, prices)
    for what, (equal, compared) in found.items():
        print(f"{what}: {equal} of {compared} daily closes equal the statement price")
    for code, kinds in bars.items():
        for what, items in kinds.items():
            line = ", ".join(
                f"{b['day'][5:]} {b['close']}/{prices.get(b['day'], {}).get(code)}" for b in items[-4:]
            )
            print(f"  {code} {what}: {line}")
    return {"bars": bars, "matches": {w: list(v) for w, v in found.items()}, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only probe of option closing-data sources")
    parser.add_argument("source", choices=["moomoo", "ibkr", "spy", "ibkr-daily"])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--allow-new", type=int, default=0, help="new Moomoo quota codes to spend")
    parser.add_argument("--config", default="config/local.contest.yaml")
    parser.add_argument("--day", type=date.fromisoformat, default=date(2026, 10, 22))
    parser.add_argument("--limit", type=int, default=4, help="IBKR: options to ask about")
    parser.add_argument(
        "--days",
        nargs="*",
        type=date.fromisoformat,
        default=[date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)],
        help="spy: days to probe",
    )
    args = parser.parse_args(argv)
    probes = {"moomoo": probe_moomoo, "ibkr": probe_ibkr, "spy": probe_spy, "ibkr-daily": probe_ibkr_daily}
    found = probes[args.source](args)
    path = ROOT / f"data/calibration/probe-{args.source}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": datetime.now().isoformat(), **found}, default=str, indent=1) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

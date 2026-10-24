"""Every daily IBKR Activity Statement still available (paper: the last 45 days), one file per day,
to tune the option-mark model on as many days as possible.

    .venv/bin/python ops/statement_history.py [--days 45] [--output data/calibration/statements]

Uses the Flex token and query saved by Setup IBKR Flex.command (.runtime/ibkr-flex.json). For each
business day, newest first, it asks the Flex Web Service for that day only (fd=td) and writes the
NAV rows and every Open Positions row (markPrice = the statement's closing price) to
<output>/YYYY-MM-DD.json. Days already on disk are skipped; IBKR allows 10 requests a minute, so days
are 15 s apart. It stops after two days in a row without a statement (the account's start) and at
any other failure: IBKR locks a token after too many failed requests (error 1025, Oct 23).
Read-only: nothing is written to the app's database and nothing trades.
"""

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge.flex import (  # noqa: E402
    NOT_AVAILABLE,
    FlexError,
    fetch_statement,
    parse_statement,
    read_flex_settings,
)

PAUSE = 15  # seconds between days: a statement takes 2-4 requests of the 10 a minute allowed
MISSING_STOP = 2  # consecutive days without a statement: the account's history has ended
ATTEMPTS = 3  # GetStatement polls per day ("generation in progress"), fewer than the app's six


def business_days(end, count):
    """The `count` weekdays up to `end`, newest first."""
    day, days = end, []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def as_json(statement):
    def plain(value):
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value if isinstance(value, (int, float, str, bool, type(None))) else str(value)

    return plain(statement)


async def backfill(token, query, days, folder, fetch=fetch_statement, sleep=asyncio.sleep):
    """Newest first. A day without a statement (IBKR 1003, or an empty one: a holiday, or before the
    account existed) is skipped; MISSING_STOP of them in a row end the run. Any other failure
    (token locked by too many failed requests (1025), network, ...) ends it at once: failed
    requests are what lock a token, and a later run resumes from the files on disk."""
    folder.mkdir(parents=True, exist_ok=True)
    done, failed, missing, asked = [], [], 0, False
    for day in days:
        path = folder / f"{day}.json"
        if path.exists():
            missing = 0
            continue
        if asked:
            await sleep(PAUSE)
        asked = True
        try:
            statement = parse_statement(
                await fetch(token, query, from_date=day, to_date=day, attempts=ATTEMPTS)
            )
        except FlexError as exc:
            failed.append((day, str(exc)))
            print(f"{day}: {exc}", flush=True)
            if exc.code != NOT_AVAILABLE:
                print("Stopped: run again later; days already written are skipped", flush=True)
                break
            statement = None
        except ValueError as exc:
            failed.append((day, str(exc)))
            print(f"{day}: {exc}; stopped", flush=True)
            break
        if statement is None or (not statement["nav"].get(day) and not statement["positions"]):
            if statement is not None:
                failed.append((day, "no statement data (holiday or before the account existed)"))
                print(f"{day}: no statement data", flush=True)
            missing += 1
            if missing >= MISSING_STOP:
                print(f"Stopped: {missing} days in a row without a statement", flush=True)
                break
            continue
        missing = 0
        path.write_text(json.dumps({"date": str(day), **as_json(statement)}, indent=1) + "\n")
        options = sum(p["asset"] == "OPT" for p in statement["positions"])
        print(
            f"{day}: NAV {statement['nav'].get(day, {}).get('total')}, {options} option positions", flush=True
        )
        done.append(day)
    return done, failed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--output", default=str(ROOT / "data/calibration/statements"))
    args = parser.parse_args(argv)
    settings = read_flex_settings(ROOT)
    if settings is None:
        raise SystemExit("No Flex token and query: double-click Setup IBKR Flex.command first")
    done, failed = asyncio.run(backfill(*settings, business_days(args.end, args.days), Path(args.output)))
    print(f"{len(done)} statements written, {len(failed)} days without one; folder {args.output}")


if __name__ == "__main__":
    main()

"""Every daily IBKR Activity Statement still available (paper: the last 45 days), one file per day,
to tune the option-mark model on as many days as possible.

    .venv/bin/python ops/statement_history.py [--days 45] [--output data/calibration/statements]

Uses the Flex token and query saved by Setup IBKR Flex.command (.runtime/ibkr-flex.json). For each
business day it asks the Flex Web Service for that day only (fd=td) and writes the NAV rows and every
Open Positions row (markPrice = the statement's closing price) to <output>/YYYY-MM-DD.json. Days
already on disk are skipped; IBKR allows 10 requests a minute, so days are 15 s apart. Read-only:
nothing is written to the app's database and nothing trades.
"""

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge.flex import FlexError, fetch_statement, parse_statement, read_flex_settings  # noqa: E402

PAUSE = 15  # seconds between days: a statement takes 2-4 requests of the 10 a minute allowed


def business_days(end, count):
    day, days = end, []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def as_json(statement):
    def plain(value):
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value if isinstance(value, (int, float, str, bool, type(None))) else str(value)

    return plain(statement)


async def backfill(token, query, days, folder, fetch=fetch_statement, sleep=asyncio.sleep):
    folder.mkdir(parents=True, exist_ok=True)
    done, failed = [], []
    for day in days:
        path = folder / f"{day}.json"
        if path.exists():
            continue
        if done or failed:
            await sleep(PAUSE)
        try:
            statement = parse_statement(await fetch(token, query, from_date=day, to_date=day))
        except (FlexError, ValueError) as exc:
            failed.append((day, str(exc)))
            print(f"{day}: {exc}", flush=True)
            continue
        if not statement["nav"].get(day) and not statement["positions"]:
            failed.append((day, "no statement data (holiday or before the account existed)"))
            print(f"{day}: no statement data", flush=True)
            continue
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

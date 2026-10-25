"""Past days' OCC option prices from the Moomoo collector's archives, for tuning hedge/occ_marks.py on
more days than the statements cover. Read-only (local files only).

    .venv/bin/python ops/archive_prev_close.py [--archive data/moomoo] [--undated data/moomoo/raw.jsonl] \\
        [--statements data/calibration/statements] [--output data/calibration/archive-dataset.json]

Every Moomoo quote of an option carries prev_close_price: the previous trading day's OCC price (it
equals the statement's Close Price on every held leg checked, Oct 22). ops/record_moomoo.py archives
each watched code's latest quote about every 5 s (data/moomoo/raw-YYYYMMDD.jsonl.gz), so each
archived day D gives the day before's OCC price of every option the app watched that day. Only a
quote pushed on a weekday D after Moomoo's morning roll counts ("push"; Moomoo rolls only on trading
days, so a weekend quote still carries Thursday's price); a quote from the collector's
subscription snapshot has no time and may predate the roll, so it is kept apart ("snapshot") until
the statements confirm it. An archive not named by day (raw.jsonl, the Oct 7-19 practice run) is
first split by the New York day each line was received into --split-dir.

The output has ops/mark_backtest.py's --dataset format: under each archived day D an "option_chain"
whose prev_close prices are the previous weekday's truth, and under that previous weekday the
16:00 books the same archive recorded (SPY-style options too: their OCC prices follow the 16:00
book). "check" compares the prices with every statement's held legs.
"""

import argparse
import gzip
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge.config import NY  # noqa: E402
from hedge.moomoo_archive import RECEIVED, _lines, _snapshots, archive_path, last_books  # noqa: E402

ROLLED = time(9, 31)  # Moomoo rolls options' prev_close by 09:25 (SPY's by 07:30)


def previous_weekday(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _pushed(value):
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(NY)


def _price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _received_day(line):
    """The New York day an archive line was received; None if it does not say."""
    start = line.find(RECEIVED, 0, 500)
    if start >= 0:
        end = line.find(b'"', start + len(RECEIVED), start + 60)
        text = line[start + len(RECEIVED) : end].decode() if end > 0 else ""
    else:
        try:
            text = json.loads(line).get("received_at", "")
        except (ValueError, AttributeError):
            return None
    at = _pushed(text) if text else None
    return at.date() if at else None


def split_undated(paths, directory):
    """Write every line of collector archives not named by day to directory/raw-YYYYMMDD.jsonl.gz,
    by the New York day it was received (streamed: raw.jsonl is 1.2 GB). Returns the days written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handles = {}
    try:
        for path in paths:
            for line in _lines(path):
                day = _received_day(line)
                if day is None:
                    continue
                if day not in handles:
                    handles[day] = gzip.open(directory / f"raw-{day:%Y%m%d}.jsonl.gz", "wb")
                handles[day].write(line.rstrip(b"\n") + b"\n")
    finally:
        for handle in handles.values():
            handle.close()
    return sorted(handles)


def archives(directories):
    """{day: archive path} of the day-named archives in `directories` (the first directory wins)."""
    found = {}
    for directory in directories:
        for path in sorted(Path(directory).glob("raw-*.jsonl*")):
            stamp = path.name[4:12]
            if stamp.isdigit():
                day = datetime.strptime(stamp, "%Y%m%d").date()
                found.setdefault(day, archive_path(directory, day))
    return found


def day_prices(path, day):
    """{code: {"prev_close", "kind", "at"}} from one archived day: each code's last quote pushed
    after the roll ("push"), else its subscription snapshot's ("snapshot", no time)."""
    after, found = datetime.combine(day, ROLLED, NY), {}
    for securities in _snapshots(path):
        for code, item in securities.items():
            rows = item.get("quote") if isinstance(item, dict) else None
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            price = _price(row.get("prev_close_price"))
            if price is None:
                continue
            pushed = _pushed(item.get("quote_push_at")) if item.get("quote_push_at") else None
            if pushed is not None and pushed >= after and pushed.date() == day:
                found[code] = {"prev_close": price, "kind": "push", "at": pushed.isoformat()}
            elif pushed is None and found.get(code, {}).get("kind") != "push":
                found[code] = {"prev_close": price, "kind": "snapshot", "at": None}
    return found


def is_option(code):
    return any(ch.isdigit() for ch in code)


def _symbol(code):
    body = code[3:]
    return body[: next(i for i, ch in enumerate(body) if ch.isdigit())] if is_option(code) else body


def statement_marks(folder):
    """{day: {code: Close Price}} of every held option on a saved statement."""
    marks = {}
    for path in sorted(Path(folder).glob("*.json")) if folder else []:
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") == "OPT" and p.get("mark") not in (None, ""):
                code = "US.{}{}{}{}".format(
                    p["symbol"].split()[0],
                    p["expiry"].replace("-", "")[2:],
                    p["put_call"],
                    round(float(p["strike"]) * 1000),
                )
                marks.setdefault(data["date"], {})[code] = float(p["mark"])
    return marks


def build(archive_dirs, statements, include_snapshots=False):
    paths = archives(archive_dirs)
    days = sorted(paths)
    dataset, held = {}, statement_marks(statements)
    check = {"push": [0, 0], "snapshot": [0, 0], "misses": []}
    for day in days:
        if day.weekday() >= 5:  # a weekend quote still carries Thursday's price, not Friday's
            continue
        prices = day_prices(paths[day], day)
        truth_day = previous_weekday(day)
        options = [
            {"code": code, "prev_close": p["prev_close"], "kind": p["kind"], "pushed_at": p["at"]}
            for code, p in sorted(prices.items())
            if is_option(code)
        ]
        shares = {_symbol(c): p["prev_close"] for c, p in prices.items() if not is_option(c)}
        dataset.setdefault(str(day), {})["option_chain"] = [
            {
                "source": f"Moomoo collector archive {day}: prev_close = {truth_day} OCC price",
                "options": [o for o in options if include_snapshots or o["kind"] == "push"],
                "snapshot_options": [o for o in options if o["kind"] == "snapshot"],
                "official_closes": shares,
            }
        ]
        for code, price in held.get(str(truth_day), {}).items():
            got = prices.get(code)
            if got:
                hit = abs(got["prev_close"] - price) < 1e-4
                check[got["kind"]][0] += hit
                check[got["kind"]][1] += 1
                if not hit:
                    check["misses"].append([str(truth_day), code, price, got["prev_close"], got["kind"]])
        # The truth day's own 16:00 books, from its archive, for every code priced above.
        codes = [o["code"] for o in options if include_snapshots or o["kind"] == "push"]
        if codes:
            close = datetime.combine(truth_day, time(16), NY)
            cutoffs = {code: close for code in [*codes, *{f"US.{_symbol(c)}" for c in codes}]}
            books = last_books(paths[truth_day], cutoffs) if truth_day in paths else {}
            archive = dataset.setdefault(str(truth_day), {}).setdefault("archive", {})
            for code, book in books.items():
                archive.setdefault("16:00", {})[code] = {
                    "bid": book["bid"],
                    "ask": book["ask"],
                    "at": book["at"].isoformat(),
                }
    return {
        "exported_at": datetime.now(NY).isoformat(),
        "archive_days": [str(d) for d in days],
        "days": dataset,
        "check": {
            "push": {"equal": check["push"][0], "compared": check["push"][1]},
            "snapshot": {"equal": check["snapshot"][0], "compared": check["snapshot"][1]},
            "misses": check["misses"][:50],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Past OCC option prices from the Moomoo archives")
    parser.add_argument("--archive", default=str(ROOT / "data/moomoo"))
    parser.add_argument(
        "--undated", nargs="*", help="archives not named by day (default: <archive>/raw.jsonl)"
    )
    parser.add_argument("--split-dir", help="where --undated is split by day (default: <archive>/split)")
    parser.add_argument(
        "--include-snapshots",
        action="store_true",
        help="count subscription-snapshot prices too (only once the check shows they equal the statements)",
    )
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument("--output", default=str(ROOT / "data/calibration/archive-dataset.json"))
    args = parser.parse_args(argv)
    undated = args.undated
    if undated is None:
        undated = [path for path in [Path(args.archive) / "raw.jsonl"] if path.exists()]
    directories = [args.archive]
    if undated:
        split_dir = args.split_dir or str(Path(args.archive) / "split")
        days = split_undated(undated, split_dir)
        print(f"split {len(undated)} undated archive(s) into {len(days)} days in {split_dir}", flush=True)
        directories.append(split_dir)
    result = build(directories, args.statements, args.include_snapshots)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(result) + "\n")
    temporary.replace(output)
    for day, entry in sorted(result["days"].items()):
        chain = (entry.get("option_chain") or [{}])[0]
        books = sum(len(v) for v in (entry.get("archive") or {}).values())
        print(
            f"{day}: {len(chain.get('options', []))} pushed and {len(chain.get('snapshot_options', []))}"
            f" snapshot prices for {previous_weekday(date.fromisoformat(day))}; {books} books on {day}"
        )
    print("check against the statements:", json.dumps(result["check"]["push"]), "pushed,", end=" ")
    print(json.dumps(result["check"]["snapshot"]), "snapshot")
    for miss in result["check"]["misses"][:10]:
        print("   differs:", miss)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()

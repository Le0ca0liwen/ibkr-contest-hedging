"""Past days' OCC option prices from the Moomoo collector's archives, for tuning hedge/occ_marks.py on
more days than the statements cover. Read-only (local files only).

    .venv/bin/python ops/archive_prev_close.py [--archive data/moomoo] \\
        [--statements data/calibration/statements] [--output data/calibration/archive-dataset.json]

Every Moomoo quote of an option carries prev_close_price: the previous trading day's OCC price (it
equals the statement's Close Price on every held leg checked, Oct 22). ops/record_moomoo.py archives
each watched code's latest quote about every 5 s (data/moomoo/raw-YYYYMMDD.jsonl.gz), so each
archived day D gives the day before's OCC price of every option the app watched that day. Only a
quote pushed on D after Moomoo's morning roll counts ("push"); a quote from the collector's
subscription snapshot has no time and may predate the roll, so it is kept apart ("snapshot") until
the statements confirm it.

The output has ops/mark_backtest.py's --dataset format: under each archived day D an "option_chain"
whose prev_close prices are the previous weekday's truth, and under that previous weekday the
16:00 books the same archive recorded (SPY-style options too: their OCC prices follow the 16:00 book). "check" compares the prices
with every statement's held legs.
"""

import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge.config import NY  # noqa: E402
from hedge.moomoo_archive import _snapshots, archive_path, last_books  # noqa: E402

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


def build(archive_dir, statements):
    days = sorted(
        datetime.strptime(path.name[4:12], "%Y%m%d").date()
        for path in Path(archive_dir).glob("raw-*.jsonl*")
        if path.name[4:12].isdigit()
    )
    days = sorted(set(days))
    dataset, held = {}, statement_marks(statements)
    check = {"push": [0, 0], "snapshot": [0, 0], "misses": []}
    for day in days:
        prices = day_prices(archive_path(archive_dir, day), day)
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
                "options": [o for o in options if o["kind"] == "push"],
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
        codes = [o["code"] for o in options if o["kind"] == "push"]
        if codes:
            close = datetime.combine(truth_day, time(16), NY)
            cutoffs = {code: close for code in [*codes, *{f"US.{_symbol(c)}" for c in codes}]}
            books = last_books(archive_path(archive_dir, truth_day), cutoffs)
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
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument("--output", default=str(ROOT / "data/calibration/archive-dataset.json"))
    args = parser.parse_args(argv)
    result = build(args.archive, args.statements)
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

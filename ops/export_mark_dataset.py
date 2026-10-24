"""Read-only export of everything the app recorded about past closes, to tune the option-mark model.

    .venv/bin/python ops/export_mark_dataset.py [--database URL] [--output PATH]

Per session it writes the stored close-mark captures, option chains, official share closes, imported
statements (pasted or Flex) and the ledger's closing NAVs, plus, from the Moomoo collector's
archives (data/moomoo/raw-YYYYMMDD.jsonl[.gz]), the last book at or before 16:00 and 16:15 ET of
every archived code. Never writes to the database. The default database is the contest one
(HEDGE_DATABASE_URL, else the local PostgreSQL at 127.0.0.1:55432/hedge_contest).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import bindparam, create_engine, text  # noqa: E402

from hedge.config import NY  # noqa: E402
from hedge.moomoo_archive import _lines, last_books  # noqa: E402

CATEGORIES = (
    "close_marks",
    "option_chain",
    "official_closes",
    "flex_statement",
    "statement_positions",
    "closing_capture",
)
ARCHIVE = re.compile(r"raw-(\d{8})\.jsonl(\.gz)?$")


def database_url(explicit):
    if explicit or os.environ.get("HEDGE_DATABASE_URL"):
        return explicit or os.environ["HEDGE_DATABASE_URL"]
    password = (ROOT / ".runtime/db-password").read_text().strip()
    return f"postgresql+psycopg://hedge:{quote(password, safe='')}@127.0.0.1:55432/hedge_contest"


def records(engine):
    """{date: {category: [payload, ...]}} for every record that names a session date."""
    days = {}
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT category, account, at, payload FROM records WHERE category IN :c ORDER BY at"
            ).bindparams(bindparam("c", expanding=True)),
            {"c": list(CATEGORIES)},
        )
        for category, account, at, payload in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            day = payload.get("date")
            if day:
                days.setdefault(day, {}).setdefault(category, []).append(
                    {"account": account, "stored_at": str(at), **payload}
                )
        ledger = connection.execute(
            text("SELECT account, day, nav, source, at FROM closing_nav_revisions ORDER BY at")
        ).all()
    for account, day, nav, source, at in ledger:
        days.setdefault(str(day), {}).setdefault("ledger", []).append(
            {"account": account, "nav": str(nav), "source": source, "at": str(at)}
        )
    return days


def archived_codes(path):
    codes = set()
    for i, line in enumerate(_lines(path)):
        try:
            codes.update(json.loads(line).get("securities", {}))
        except (ValueError, AttributeError):
            continue
        if i > 50:
            break
    return sorted(codes)


def archive_books(directory):
    """{date: {"16:00": {code: book}, "16:15": {...}}} from every collector archive."""
    result = {}
    for path in sorted(Path(directory).glob("raw-*.jsonl*")):
        match = ARCHIVE.search(path.name)
        if not match:
            continue
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
        codes = archived_codes(path)
        books = {}
        for label, at in (("16:00", time(16, 0)), ("16:15", time(16, 15))):
            cutoff = datetime.combine(day, at, NY)
            found = last_books(path, {code: cutoff for code in codes}, timedelta(minutes=30))
            books[label] = {
                code: {"bid": b["bid"], "ask": b["ask"], "at": b["at"].isoformat() if b["at"] else None}
                for code, b in found.items()
            }
        result[str(day)] = {"codes": codes, **books}
        print(f"{path.name}: {len(codes)} codes, {len(books['16:00'])} books at 16:00", flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database")
    parser.add_argument("--archives", default=str(ROOT / "data/moomoo"))
    parser.add_argument("--output", default=str(ROOT / "data/calibration/marks-dataset.json"))
    args = parser.parse_args(argv)
    engine = create_engine(database_url(args.database))
    days = records(engine)
    for day, books in archive_books(args.archives).items():
        days.setdefault(day, {})["archive"] = books
    for day in sorted(days):
        kinds = {
            k: (len(v) if isinstance(v, list) else len(v.get("codes", []))) for k, v in days[day].items()
        }
        print(day, kinds)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exported_at": datetime.now(NY).isoformat(), "days": days}, default=str))
    print(f"Wrote {path} ({path.stat().st_size // 1024} KB). Read-only: nothing was changed.")


if __name__ == "__main__":
    main()

import gzip
import json
from datetime import datetime, timezone

import pytest

from hedge.config import NY
from ops.archive_prev_close import build
from ops.mark_backtest import load_books, load_truth

PUT, CALL, STALE, STOCK = "US.RKLB261120P55000", "US.RKLB261120C60000", "US.RKLB261120P40000", "US.RKLB"


def ny(day, hour, minute):
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


def item(prev_close, pushed=None, book=None):
    entry = {"quote": [{"code": "x", "prev_close_price": prev_close}]}
    if pushed is not None:
        entry["quote_push_at"] = pushed.astimezone(timezone.utc).isoformat()
    if book is not None:
        bid, ask, at = book
        entry["order_book"] = {"Bid": [[bid, 1, 1, []]], "Ask": [[ask, 1, 1, []]]}
        entry["book_push_at"] = at.astimezone(timezone.utc).isoformat()
    return entry


def write(directory, day, snapshots):
    path = directory / f"raw-202610{day}.jsonl.gz"
    for received, securities in snapshots:
        with gzip.open(path, "at") as archive:  # one gzip member per append, as the collector writes
            line = {"received_at": received.astimezone(timezone.utc).isoformat(), "securities": securities}
            archive.write(json.dumps(line) + "\n")


def test_each_archived_day_gives_the_previous_days_prices_and_that_days_books(tmp_path):
    archive, statements = tmp_path / "moomoo", tmp_path / "statements"
    archive.mkdir()
    statements.mkdir()
    # Oct 22: the 16:00 books (and quotes still carrying Oct 21's prices).
    write(
        archive,
        22,
        [
            (
                ny(22, 15, 59),
                {
                    PUT: item(5.1504, ny(22, 15, 59), (6.55, 7.40, ny(22, 15, 59))),
                    STOCK: item(53.67, ny(22, 15, 59), (51.22, 51.24, ny(22, 15, 59))),
                },
            )
        ],
    )
    # Oct 23: quotes pushed after the roll carry Oct 22's OCC prices.
    write(
        archive,
        23,
        [
            (
                ny(23, 9, 26),
                {
                    PUT: item(6.0, ny(23, 9, 20)),  # pushed before the roll: still Oct 21's price
                    CALL: item(0.85),  # the subscription snapshot: no push time
                    STALE: item(0.40, ny(23, 9, 0)),  # never pushed after the roll
                },
            ),
            (ny(23, 10, 0), {PUT: item(6.55, ny(23, 10, 0)), STOCK: item(51.22, ny(23, 10, 0))}),
        ],
    )
    leg = {
        "asset": "OPT",
        "symbol": "RKLB  261120P00055000",
        "expiry": "2026-11-20",
        "strike": "55",
        "put_call": "P",
        "quantity": "25",
        "mark": "6.55",
    }
    (statements / "2026-10-22.json").write_text(json.dumps({"date": "2026-10-22", "positions": [leg]}))
    result = build(archive, statements)
    chain = result["days"]["2026-10-23"]["option_chain"][0]
    assert [(o["code"], o["prev_close"]) for o in chain["options"]] == [(PUT, 6.55)]
    assert [o["code"] for o in chain["snapshot_options"]] == [CALL] and chain["official_closes"] == {
        "RKLB": 51.22
    }
    assert result["check"]["push"] == {"equal": 1, "compared": 1}
    # The backtest reads it as a --dataset: Oct 22's truth and Oct 22's own 16:00 books.
    truth, conids = load_truth(None, result)
    assert truth["2026-10-22"][PUT]["mark"] == 6.55
    books, spots = load_books(result, conids, [])
    assert books["2026-10-22"][PUT] == (6.55, 7.40) and spots["2026-10-22"]["RKLB"] == pytest.approx(51.23)

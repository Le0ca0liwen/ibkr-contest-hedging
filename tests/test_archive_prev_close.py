import gzip
import json
from datetime import datetime, timezone

import pytest

from hedge.config import NY
from ops.archive_prev_close import build, split_undated
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
    result = build([archive], statements)
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


def test_an_archive_not_named_by_day_is_split_by_the_day_each_line_arrived(tmp_path):
    undated = tmp_path / "raw.jsonl"
    lines = [
        (ny(15, 15, 59), {PUT: item(1.0, ny(15, 15, 59), (1.10, 1.20, ny(15, 15, 59)))}),
        (ny(16, 10, 0), {PUT: item(1.15, ny(16, 10, 0))}),
        (ny(16, 15, 59), {PUT: item(1.15, ny(16, 15, 59), (0.90, 1.00, ny(16, 15, 59)))}),
    ]
    undated.write_text(
        "".join(
            json.dumps({"received_at": at.astimezone(timezone.utc).isoformat(), "securities": securities})
            + "\n"
            for at, securities in lines
        )
    )
    days = split_undated([undated], tmp_path / "split")
    assert [f"{d:%m-%d}" for d in days] == ["10-15", "10-16"]
    result = build([tmp_path / "split"], None)
    assert result["days"]["2026-10-16"]["option_chain"][0]["options"][0]["prev_close"] == 1.15
    assert result["days"]["2026-10-15"]["archive"]["16:00"][PUT]["bid"] == 1.10


def test_a_weekend_archive_gives_no_prices(tmp_path):
    """Oct 10-11 (Sat/Sun): quotes still carried Thursday Oct 8's prices, labelled Friday's."""
    archive = tmp_path / "moomoo"
    archive.mkdir()
    write(archive, 10, [(ny(10, 10, 0), {PUT: item(1.045, ny(10, 10, 0))})])
    write(archive, 12, [(ny(12, 10, 0), {PUT: item(1.315, ny(12, 10, 0))})])
    result = build([archive], None)
    assert "2026-10-10" not in result["days"]
    assert result["days"]["2026-10-12"]["option_chain"][0]["options"][0]["prev_close"] == 1.315
    truth, _ = load_truth(None, result)
    assert truth["2026-10-09"][PUT]["mark"] == 1.315


def test_a_book_from_well_before_the_close_is_not_taken_as_the_16_00_book(tmp_path):
    archive = tmp_path / "moomoo"
    archive.mkdir()
    write(archive, 15, [(ny(15, 15, 32), {PUT: item(1.0, ny(15, 15, 32), (7.10, 7.60, ny(15, 15, 32)))})])
    write(archive, 16, [(ny(16, 10, 0), {PUT: item(6.39, ny(16, 10, 0))})])
    result = build([archive], None)
    assert "archive" not in result["days"]["2026-10-15"]  # the collector stopped at 15:32
    assert result["days"]["2026-10-16"]["option_chain"][0]["options"][0]["prev_close"] == 6.39

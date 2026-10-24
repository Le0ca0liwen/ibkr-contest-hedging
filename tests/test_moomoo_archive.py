import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from hedge import moomoo_archive
from hedge.config import NY
from hedge.moomoo_archive import archive_path, final_books, last_books
from ops.fetch_moomoo_chain import fetch_chain, main

DAY = date(2026, 10, 23)
RKLB, SPY, SPCX, STOCK = "US.RKLB261120P45000", "US.SPY261120C700000", "US.SPCX261120C130000", "US.RKLB"


def at(hour, minute, second=0, micro=0):
    return datetime(2026, 10, 23, hour, minute, second, micro, tzinfo=NY)


def item(bid, ask, bid_time="", ask_time="", pushed=None, count=2):
    entry = {
        "quote": [{"code": "x"}],
        "order_book": {
            "code": "x",
            "Bid": [[bid, 10, 1, []]],
            "Ask": [[ask, 12, 2, []]],
            "svr_recv_time_bid": bid_time,
            "svr_recv_time_ask": ask_time,
        },
        "book_push_count": count,
    }
    if pushed is not None:
        entry["book_push_at"] = pushed.astimezone(timezone.utc).isoformat()
    return entry


def snapshot(received, securities):
    return {
        "provider": "moomoo",
        "purpose": "market_data_only",
        "execution_enabled": False,
        "received_at": received.astimezone(timezone.utc).isoformat(),
        "securities": securities,
    }


def lines():
    """Books at 15:55:00, 15:56:16 and 16:01:00 New York (a corrupt line between them)."""
    first, second, third = at(15, 55), at(15, 56, 16, 219000), at(16, 1)
    return [
        snapshot(
            first,
            {
                RKLB: item(1.90, 1.96, "2026-10-23 15:54:58.100", "2026-10-23 15:55:00.000"),
                SPY: item(10.70, 10.80, "2026-10-23 15:55:00.000", ""),
                SPCX: item(18.35, 18.55, "", "", pushed=first),
                STOCK: item(51.20, 51.24, "2026-10-23 15:55:00.000", "2026-10-23 15:55:00.000"),
            },
        ),
        snapshot(
            second,
            {
                RKLB: item(1.91, 1.95, "2026-10-23 15:50:00.000", "2026-10-23 15:56:16.219"),
                SPY: item(10.90, 10.80, "2026-10-23 15:56:16.219", "2026-10-23 15:56:16.219"),
                SPCX: item(18.40, 18.50, "", "", pushed=second),
                STOCK: item(0, 51.24, "2026-10-23 15:56:16.219", "2026-10-23 15:56:16.219"),
            },
        ),
        snapshot(
            third,
            {
                RKLB: item(1.80, 1.84, "2026-10-23 16:01:00.000", "2026-10-23 16:01:00.000"),
                SPY: item(10.72, 10.76, "2026-10-23 16:01:00.000", "2026-10-23 16:01:00.000"),
                SPCX: item(18.10, 18.30, "", "", pushed=third),
            },
        ),
    ]


def write_archive(directory, compressed=True):
    path = directory / ("raw-20261023.jsonl.gz" if compressed else "raw-20261023.jsonl")
    encoded = [json.dumps(raw) + "\n" for raw in lines()]
    encoded.insert(1, '{"received_at": "2026-10-23T19:55:30+00:00", "securities": {broken\n')
    if compressed:
        for line in encoded:
            with gzip.open(path, "at") as archive:  # one gzip member per append, as the collector writes
                archive.write(line)
    else:
        path.write_text("".join(encoded))
    return path


def test_last_books_takes_the_last_valid_book_before_each_cutoff(tmp_path):
    path = write_archive(tmp_path)
    assert path.name == "raw-20261023.jsonl.gz"
    books = last_books(path, {RKLB: at(16, 0), SPCX: at(16, 0), STOCK: at(16, 0), SPY: at(16, 0)})
    # The later side's receipt time dates the book; the 16:01 book is after the cutoff.
    assert books[RKLB] == {"bid": 1.91, "ask": 1.95, "at": at(15, 56, 16, 219000), "source": "archive"}
    assert books[RKLB]["at"].tzinfo is not None
    # No exchange times: the collector's push time (UTC) dates it.
    assert books[SPCX]["bid"] == 18.40 and books[SPCX]["at"] == at(15, 56, 16, 219000)
    # A crossed book and a zero bid are skipped: the earlier valid book stands.
    assert books[SPY]["bid"] == 10.70 and books[SPY]["at"] == at(15, 55)
    assert books[STOCK]["bid"] == 51.20 and books[STOCK]["at"] == at(15, 55)


def test_last_books_respects_max_age_and_per_code_cutoffs(tmp_path):
    path = write_archive(tmp_path)
    books = last_books(path, {RKLB: at(16, 0), SPY: at(16, 2)}, max_age=timedelta(minutes=3))
    assert RKLB not in books  # 15:56:16 is older than 15:57
    assert books[SPY] == {"bid": 10.72, "ask": 10.76, "at": at(16, 1), "source": "archive"}
    assert last_books(path, {SPY: at(16, 15)})[SPY]["at"] == at(16, 1)  # SPY options close at 16:15
    assert last_books(path, {RKLB: at(15, 50)}) == {}
    assert last_books(path, {RKLB: at(16, 1, 30)}, max_age=timedelta(minutes=1))[RKLB]["at"] == at(16, 1)
    assert last_books(path, {"US.NONE": at(16, 0)}) == {}


def test_last_books_skips_only_lines_received_before_every_window(tmp_path, monkeypatch):
    path, parsed = write_archive(tmp_path), []

    def loads(line):
        parsed.append(line)
        return json.loads(line)

    monkeypatch.setattr(moomoo_archive, "json", SimpleNamespace(loads=loads))
    # Windows open at 16:00:30; less SKEW, 15:55:30: the 15:55:00 line is skipped, the corrupt
    # 15:55:30 line and the later ones are parsed.
    books = last_books(path, {RKLB: at(16, 1, 30)}, max_age=timedelta(minutes=1))
    assert books[RKLB]["at"] == at(16, 1) and len(parsed) == 3
    parsed.clear()
    assert last_books(path, {RKLB: at(16, 0)})[RKLB]["at"] == at(15, 56, 16, 219000)
    assert len(parsed) == 4


def test_last_books_resumes_after_a_damaged_member(tmp_path):
    """The collector stopped mid-append (app restart) and a new collector appended after it."""
    first, damaged, third = (gzip.compress((json.dumps(raw) + "\n").encode()) for raw in lines()[:3])
    later = snapshot(at(15, 58), {RKLB: item(1.88, 1.92, "2026-10-23 15:58:00.000", "")})
    resumed = gzip.compress((json.dumps(later) + "\n").encode())
    for cut in (5, 20, len(damaged) // 2, len(damaged) - 12):  # the trailer is the last 8 bytes
        path = tmp_path / f"raw-{cut}.jsonl"  # gzip content under a plain name: told apart by content
        path.write_bytes(first + damaged[:cut] + resumed + third)
        books = last_books(path, {RKLB: at(16, 0), SPCX: at(16, 0)})
        assert books[RKLB] == {"bid": 1.88, "ask": 1.92, "at": at(15, 58), "source": "archive"}
        assert books[SPCX]["at"] == at(15, 55)  # the damaged line's book is lost, not misread


def test_zero_receipt_times_fall_back_to_the_push_time(tmp_path):
    zero = item(1.90, 1.96, "1969-12-31 19:00:00.000", "1970-01-01 00:00:00.000", pushed=at(15, 59))
    path = tmp_path / "raw.jsonl"
    path.write_text(json.dumps(snapshot(at(15, 59, 5), {RKLB: zero})) + "\n")
    assert last_books(path, {RKLB: at(16, 0)})[RKLB]["at"] == at(15, 59)


def test_last_books_reads_plain_archives_and_tolerates_missing_or_truncated_files(tmp_path):
    plain = write_archive(tmp_path, compressed=False)
    assert archive_path(tmp_path, DAY) == plain
    late = datetime(2026, 10, 24, 3, 30, tzinfo=timezone.utc)
    assert archive_path(tmp_path, late) == plain  # a datetime counts as its New York day
    assert last_books(plain, {RKLB: at(16, 0)})[RKLB]["ask"] == 1.95
    assert last_books(tmp_path / "raw-20261022.jsonl.gz", {RKLB: at(16, 0)}) == {}
    compressed = write_archive(tmp_path)
    assert compressed.name == "raw-20261023.jsonl.gz"
    assert archive_path(tmp_path, DAY) == compressed  # the .gz wins when both exist
    data = compressed.read_bytes()
    truncated = tmp_path / "cut.jsonl.gz"
    truncated.write_bytes(data[: len(data) - 40])  # the collector was mid-append
    books = last_books(truncated, {RKLB: at(16, 0)})
    assert books[RKLB]["at"] == at(15, 56, 16, 219000)


def test_final_books_reads_the_sessions_last_valid_books(tmp_path):
    pushed = at(16, 0, 2)
    raw = snapshot(
        at(18, 30),
        {
            RKLB: item(1.92, 1.94, "2026-10-23 15:59:59.500", "2026-10-23 15:59:59.800", pushed=pushed),
            SPY: item(10.80, 10.70, pushed=pushed),
            SPCX: item(0, 18.50, pushed=pushed),
            STOCK: item(51.10, 51.30, "2026-10-23 15:59:59.000", "2026-10-23 16:00:00.000"),
            "US.BROKEN": {"error": "Subscription failed"},
        },
    )
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(raw))
    books = final_books(path, [RKLB, SPY, SPCX, STOCK, "US.BROKEN", "US.NONE"])
    assert set(books) == {RKLB, STOCK}
    assert books[RKLB] == {"bid": 1.92, "ask": 1.94, "at": pushed, "source": "final book"}
    assert books[STOCK]["at"] == at(16, 0)  # no push time: the exchange receipt time
    assert final_books(tmp_path / "missing.json", [RKLB]) == {}
    path.write_text("{not json")
    assert final_books(path, [RKLB]) == {}


class FakeContext:
    """Quote methods only: any trade or account call would fail."""

    def __init__(self, chains, spots, fail=()):
        self.chains, self.spots, self.fail, self.calls = chains, spots, set(fail), []

    def get_market_snapshot(self, codes):
        self.calls.append(("snapshot", list(codes)))
        assert len(codes) <= 400
        rows = []
        for code in codes:
            symbol = code[3:]
            if symbol in self.spots:
                last, bid, ask = self.spots[symbol]
                rows.append({"code": code, "last_price": last, "bid_price": bid, "ask_price": ask})
                continue
            option = self.chains[code]
            rows.append(
                {
                    "code": code,
                    "last_price": 1.5,
                    "bid_price": 1.4,
                    "ask_price": 1.6,
                    "update_time": "2026-10-23 16:14:59",
                    "option_strike_price": option["strike_price"],
                    "option_type": option["option_type"],
                    "strike_time": option["strike_time"],
                }
            )
        return 0, pd.DataFrame(rows)

    def get_option_chain(self, code, start=None, end=None):
        self.calls.append(("chain", code, start, end))
        if (code, start) in self.fail:
            return -1, "request too frequent"
        rows = [
            {"code": c, **o}
            for c, o in self.chains.items()
            if c.startswith(code) and o["strike_time"] == start
        ]
        return 0, pd.DataFrame(rows, columns=["code", "strike_price", "option_type", "strike_time"])


def chain(symbol, expiry, strikes):
    result = {}
    for strike in strikes:
        for right, name in (("C", "CALL"), ("P", "PUT")):
            code = f"US.{symbol}{expiry.replace('-', '')[2:]}{right}{round(strike * 1000)}"
            result[code] = {"strike_price": strike, "option_type": name, "strike_time": expiry}
    return result


def test_fetch_chain_filters_by_moneyness_and_chunks_snapshots():
    chains = {
        **chain("SPY", "2026-11-20", range(300, 901)),
        **chain("SPY", "2026-11-27", [600]),
        **chain("RKLB", "2026-11-20", range(30, 71, 5)),
    }
    ctx = FakeContext(chains, {"SPY": (600.0, 599.9, 600.1), "RKLB": (50.0, 49.9, 50.1)})
    sleeps = []
    result = fetch_chain(
        ctx,
        SimpleNamespace(RET_OK=0),
        ["SPY", "RKLB"],
        ["2026-11-20", "2026-11-27"],
        0.3,
        sleep=sleeps.append,
    )
    assert result["underlyings"] == {
        "SPY": {"spot": 600.0, "bid": 599.9, "ask": 600.1},
        "RKLB": {"spot": 50.0, "bid": 49.9, "ask": 50.1},
    }
    options = result["options"]
    spy = [o for o in options if o["underlying"] == "SPY" and o["expiry"] == "2026-11-20"]
    assert len(spy) == 2 * 361 and {o["strike"] for o in spy} == set(range(420, 781))
    rklb = [o for o in options if o["underlying"] == "RKLB"]
    assert {o["strike"] for o in rklb} == {35, 40, 45, 50, 55, 60, 65}
    assert len(options) == 722 + 2 + 14
    assert options[0] == {
        "code": "US.RKLB261120C35000",
        "underlying": "RKLB",
        "expiry": "2026-11-20",
        "strike": 35.0,
        "right": "C",
        "bid": 1.4,
        "ask": 1.6,
        "last": 1.5,
        "update_time": "2026-10-23 16:14:59",
    }
    snapshots = [call[1] for call in ctx.calls if call[0] == "snapshot"]
    assert snapshots[:2] == [["US.SPY"], ["US.RKLB"]]
    option_requests = snapshots[2:]
    assert [len(codes) for codes in option_requests] == [400, 338]
    assert sorted(c for codes in option_requests for c in codes) == sorted(o["code"] for o in options)
    assert ("chain", "US.RKLB", "2026-11-27", "2026-11-27") in ctx.calls
    assert result["errors"] == ["No options listed for US.RKLB 2026-11-27"]
    # Paced: one wait between consecutive requests, longer before an option chain request.
    assert len(sleeps) == len(ctx.calls) - 1
    kinds = [call[0] for call in ctx.calls[1:]]
    assert all(s == (3.0 if kind == "chain" else 0.6) for s, kind in zip(sleeps, kinds, strict=True))
    assert datetime.fromisoformat(result["at"]).tzinfo is not None


def test_fetch_chain_records_failed_requests_and_falls_back_to_the_midpoint():
    chains = chain("SPCX", "2026-11-20", [90, 130, 200])
    ctx = FakeContext(
        chains, {"SPCX": (None, 139.0, 141.0), "NONE": (None, 0, 0)}, fail={("US.SPCX", "2026-11-27")}
    )
    result = fetch_chain(
        ctx,
        SimpleNamespace(RET_OK=0),
        ["NONE", "SPCX"],
        ["2026-11-20", "2026-11-27"],
        0.3,
        sleep=lambda s: None,
    )
    assert result["underlyings"] == {"SPCX": {"spot": 140.0, "bid": 139.0, "ask": 141.0}}
    assert [o["code"] for o in result["options"]] == ["US.SPCX261120C130000", "US.SPCX261120P130000"]
    assert result["errors"] == [
        "No spot for US.NONE: no price",
        "No option chain for US.SPCX 2026-11-27: request too frequent",
    ]


def test_main_writes_the_chain_atomically_with_a_quote_only_context(tmp_path, monkeypatch):
    chains = chain("RKLB", "2026-11-20", [45, 55])
    opened = []

    class Context(FakeContext):
        def __init__(self, **kwargs):
            assert kwargs == {"host": "127.0.0.1", "port": 11111}
            opened.append("open")
            super().__init__(chains, {"RKLB": (50.0, 49.9, 50.1)})

        def close(self):
            opened.append("close")

    monkeypatch.setitem(sys.modules, "moomoo", SimpleNamespace(OpenQuoteContext=Context, RET_OK=0))
    monkeypatch.setattr(
        "ops.fetch_moomoo_chain.fetch_chain", lambda *a, **k: fetch_chain(*a, sleep=lambda s: None, **k)
    )
    output = tmp_path / "out" / "chain.json"
    main(
        ["--underlyings", "rklb", "--expiries", "2026-11-20", "--moneyness", "0.25", "--output", str(output)]
    )
    assert opened == ["open", "close"]
    written = json.loads(output.read_text())
    assert set(written) == {"at", "underlyings", "options", "errors"}
    assert [o["code"] for o in written["options"]] == [
        "US.RKLB261120C45000",
        "US.RKLB261120C55000",
        "US.RKLB261120P45000",
        "US.RKLB261120P55000",
    ]
    assert not list(output.parent.glob("*.tmp"))
    # A run without option quotes (OpenD down, no chain) keeps the earlier file.
    with pytest.raises(SystemExit, match="not written"):
        main(["--underlyings", "RKLB", "--expiries", "2026-11-27", "--output", str(output)])
    assert opened[-2:] == ["open", "close"] and json.loads(output.read_text()) == written

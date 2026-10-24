"""Closing books from the Moomoo collector's files, for a session whose own 16:00/16:15 capture was
missed (Oct 23: the Mac slept from 15:56).

ops/record_moomoo.py rewrites data/moomoo/latest.json every second and appends the same object about
every 5 s as one line of data/moomoo/raw-YYYYMMDD.jsonl.gz (New York day, regular session plus
margins). Each security's order book carries the exchange receipt times of its bid and ask (naive New
York wall clock, sometimes empty) and the collector's push time (UTC). After the close latest.json
keeps the session's last book: for an option, its book near the close. Reads quotes only.
"""

from __future__ import annotations

import json
import math
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import NY

SKEW = timedelta(minutes=5)  # a book's exchange receipt time against the Mac clock that wrote the line
MAGIC = b"\x1f\x8b\x08"  # a gzip (deflate) member header
RECEIVED = b'"received_at": "'


def archive_path(directory, day):
    """The day's collector archive raw-YYYYMMDD.jsonl.gz, or raw-YYYYMMDD.jsonl when only that exists.
    `day` is a date (a datetime counts as its New York day)."""
    if isinstance(day, datetime):
        day = day.astimezone(NY).date()
    compressed = Path(directory) / f"raw-{day:%Y%m%d}.jsonl.gz"
    plain = compressed.with_suffix("")
    return plain if plain.exists() and not compressed.exists() else compressed


def _top(book):
    """(bid, ask) of a valid top of book (bid > 0, ask >= bid), else None."""
    try:
        bid, ask = float(book["Bid"][0][0]), float(book["Ask"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
        return None
    return bid, ask


def _push_time(item):
    """The collector's receipt time of the book (UTC), in New York; None without one."""
    try:
        at = datetime.fromisoformat(item["book_push_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(NY)


def _book_time(item):
    """When the book last changed: the later of its bid/ask exchange receipt times (naive New York),
    else its push time. Aware, New York; None without either. A zero receipt time (OpenD sends one
    after a server restart) counts as none."""
    book, times = item["order_book"], []
    for field in ("svr_recv_time_bid", "svr_recv_time_ask"):
        try:
            at = datetime.fromisoformat(str(book.get(field) or ""))
        except ValueError:
            continue
        if at.year >= 2000:
            times.append(at.replace(tzinfo=NY) if at.tzinfo is None else at.astimezone(NY))
    return max(times) if times else _push_time(item)


def _received_before(line, since):
    """True when the line's received_at (written near its start) is before `since`; False if unknown."""
    start = line.find(RECEIVED, 0, 500)
    end = line.find(b'"', start + len(RECEIVED), start + 60) if start >= 0 else -1
    if end < 0:
        return False
    try:
        at = datetime.fromisoformat(line[start + len(RECEIVED) : end].decode())
    except ValueError:
        return False
    return at.tzinfo is not None and at < since


def _next_member(raw, offset, size):
    """File offset of the first gzip member header at or after `offset`; None without one."""
    raw.seek(offset)
    buffer = b""
    while chunk := raw.read(size):
        buffer += chunk
        found = buffer.find(MAGIC)
        if found >= 0:
            return offset + found
        kept = buffer[-2:]
        offset, buffer = offset + len(buffer) - len(kept), kept
    return None


def _gzip_lines(raw, size=1 << 16):
    """Lines of a gzip file appended one member at a time. A damaged member (the collector was
    stopped mid-append, then appended again) loses its unfinished line and reading resumes at the
    next member header after it; an unfinished last member (still being appended) is dropped."""
    start, line = 0, b""
    while start is not None:
        raw.seek(start)
        decoder, read = zlib.decompressobj(31), start
        try:
            while chunk := raw.read(size):
                read += len(chunk)
                while chunk:
                    *complete, line = (line + decoder.decompress(chunk)).split(b"\n")
                    yield from complete
                    if not decoder.eof:
                        break
                    chunk, decoder = decoder.unused_data, zlib.decompressobj(31)
                    start = read - len(chunk)
            if start == read:  # every member ended
                if line:
                    yield line
                return
        except zlib.error:
            pass
        # The member at `start` is damaged or unfinished; it may have read later members as garbage.
        line, start = b"", _next_member(raw, start + 1, size)


def _lines(path):
    """Raw lines of an archive, gzip or plain (told apart by content); none if it is missing."""
    try:
        with open(path, "rb") as raw:
            if raw.read(2) == MAGIC[:2]:
                yield from _gzip_lines(raw)
            else:
                raw.seek(0)
                yield from raw
    except OSError:
        return


def _snapshots(path, since=None):
    """The securities of each archive line received at or after `since`, skipping lines that fail
    to parse."""
    for line in _lines(path):
        if since is not None and _received_before(line, since):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict) and isinstance(raw.get("securities"), dict):
            yield raw["securities"]


def _item(securities, code):
    item = securities.get(code)
    if isinstance(item, dict) and isinstance(item.get("order_book"), dict):
        return item
    return None


def last_books(path, cutoffs, max_age=timedelta(minutes=30)):
    """{code: {"bid", "ask", "at", "source": "archive"}} from a collector archive: for each code of
    `cutoffs` (code -> aware datetime), its last valid book (bid > 0, ask >= bid) whose book time is
    within [cutoff - max_age, cutoff]. A code without one is left out; a missing archive gives {}."""
    if not cutoffs:
        return {}
    found = {}
    # A line received before every window opened holds only older books: skip it unparsed.
    for securities in _snapshots(path, min(cutoffs.values()) - max_age - SKEW):
        for code, cutoff in cutoffs.items():
            item = _item(securities, code)
            if item is None:
                continue
            at = _book_time(item)
            if at is None or not cutoff - max_age <= at <= cutoff:
                continue
            top = _top(item["order_book"])
            if top is not None and (code not in found or at >= found[code]["at"]):
                found[code] = {"bid": top[0], "ask": top[1], "at": at, "source": "archive"}
    return found


def final_books(path, codes):
    """{code: {"bid", "ask", "at", "source": "final book"}} from the collector's latest.json, valid
    books only; {} without the file. "at" is the book's push time (else its exchange receipt time;
    None without either), aware, New York. A share's final book is after-hours, never its close."""
    try:
        securities = json.loads(Path(path).read_text())["securities"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(securities, dict):
        return {}
    found = {}
    for code in codes:
        item = _item(securities, code)
        top = item and _top(item["order_book"])
        if top:
            at = _push_time(item) or _book_time(item)
            found[code] = {"bid": top[0], "ask": top[1], "at": at, "source": "final book"}
    return found

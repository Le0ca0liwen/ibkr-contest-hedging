"""Streaming quote-only worker. No moomoo trade context or account operations."""

import argparse
import csv
import fcntl
import gzip
import json
import threading
import time
from datetime import datetime, timezone
from datetime import time as clock
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ARCHIVE_DAILY_BYTES = 750_000_000
NEW_YORK = ZoneInfo("America/New_York")


def archive_path(directory, now):
    """Compressed per-New-York-day archive, regular session (+ margins) only; None outside it."""
    local = now.astimezone(NEW_YORK)
    if local.weekday() >= 5 or not clock(9, 25) <= local.time() <= clock(16, 10):
        return None
    return Path(directory) / f"raw-{local:%Y%m%d}.jsonl.gz"


def make_handlers(sdk, lock, securities, rights, stamp, bars=None):
    class Quotes(sdk.StockQuoteHandlerBase):
        def on_recv_rsp(self, rsp):
            ret, data = super().on_recv_rsp(rsp)
            if ret == sdk.RET_OK:
                with lock:
                    for row in json.loads(data.to_json(orient="records")):
                        code = row["code"]
                        item = securities.setdefault(code, {})
                        item["quote"] = [row]
                        item["quote_push_at"] = stamp()
            return ret, data

    class Books(sdk.OrderBookHandlerBase):
        def on_recv_rsp(self, rsp):
            ret, data = super().on_recv_rsp(rsp)
            if ret == sdk.RET_OK:
                with lock:
                    item = securities.setdefault(data["code"], {})
                    item["order_book"] = data
                    item["book_push_at"] = stamp()
                    item["book_push_count"] = item.get("book_push_count", 0) + 1
            return ret, data

    class Notify(sdk.SysNotifyHandlerBase):
        def on_recv_rsp(self, rsp):
            ret, data = super().on_recv_rsp(rsp)
            # The SDK returns (notify_type, sub_type, payload); quote rights are top-level payload keys.
            if ret == sdk.RET_OK and isinstance(data, tuple) and len(data) == 3:
                notify_type, _, payload = data
                if notify_type == sdk.SysNotifyType.QOT_RIGHT and isinstance(payload, dict):
                    with lock:
                        rights.update(
                            {k: v for k, v in payload.items() if k in ("us_qot_right", "us_option_qot_right")}
                        )
            return ret, data

    class Bars(sdk.CurKlineHandlerBase):
        def on_recv_rsp(self, rsp):
            ret, data = super().on_recv_rsp(rsp)
            if ret == sdk.RET_OK and bars is not None:
                with lock:
                    for row in json.loads(data.to_json(orient="records")):
                        if row.get("k_type") == "K_1M":
                            bars[(row["code"], row["time_key"])] = row
                    # Keep at most one complete current-session buffer per symbol.
                    if len(bars) > 3500:
                        for key in sorted(bars, key=lambda k: k[1])[: len(bars) - 3500]:
                            bars.pop(key)
            return ret, data

    handlers = [Quotes(), Books(), Notify()]
    if bars is not None:
        handlers.append(Bars())
    return tuple(handlers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="config/local.moomoo-codes.json")
    args = parser.parse_args()
    codes = json.loads(Path(args.codes).read_text())
    path = Path("data/moomoo/latest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "collector.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        import moomoo as sdk

        while True:
            ctx = None
            lock = threading.Lock()
            securities, rights, identities, bars = {}, {}, {}, {}
            session_id = uuid4().hex

            def stamp():
                return datetime.now(timezone.utc).isoformat()

            try:
                ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
                for handler in make_handlers(sdk, lock, securities, rights, stamp, bars):
                    ctx.set_handler(handler)
                # Requests ONLY quote permissions; never BASIC, WEBKEY, or trade/account APIs.
                ret, info = ctx.get_user_info([sdk.UserInfoField.QOTRIGHT])
                if ret == sdk.RET_OK:
                    rights.update({k: info.get(k) for k in ("us_qot_right", "us_option_qot_right")})
                ret, basic = ctx.get_stock_basicinfo(
                    sdk.Market.US,
                    sdk.SecurityType.DRVT,
                    [c for c in codes if any(ch.isdigit() for ch in c)],
                )
                if ret == sdk.RET_OK:
                    identities.update(
                        {row["code"]: row for row in json.loads(basic.to_json(orient="records"))}
                    )
                for code in codes:
                    ret, msg = ctx.subscribe(
                        [code],
                        [sdk.SubType.QUOTE, sdk.SubType.ORDER_BOOK],
                        is_first_push=False,
                        subscribe_push=True,
                    )
                    if ret != sdk.RET_OK:
                        securities[code] = {"error": "Subscription failed"}
                    ret, rows = ctx.get_stock_quote([code])
                    if ret == sdk.RET_OK:
                        with lock:
                            securities.setdefault(code, {})["quote"] = json.loads(
                                rows.to_json(orient="records")
                            )
                    ret, book = ctx.get_order_book(code, num=1)
                    if ret == sdk.RET_OK:
                        with lock:
                            securities.setdefault(code, {})["order_book"] = book
                stocks = [code for code in codes if not any(ch.isdigit() for ch in code)]
                ctx.subscribe(stocks, [sdk.SubType.K_1M], is_first_push=True, subscribe_push=True)
                for code in stocks:
                    ret, recent = ctx.get_cur_kline(
                        code, num=390, ktype=sdk.KLType.K_1M, autype=sdk.AuType.NONE
                    )
                    if ret == sdk.RET_OK:
                        with lock:
                            for row in json.loads(recent.to_json(orient="records")):
                                bars[(row["code"], row["time_key"])] = row
                last_archive = 0
                capped = None
                while True:
                    ret, state = ctx.get_global_state()
                    if ret != sdk.RET_OK or not state.get("qot_logined"):
                        raise ValueError("Quote server disconnected")
                    with lock:
                        result = {
                            "provider": "moomoo",
                            "purpose": "market_data_only",
                            "execution_enabled": False,
                            "session_id": session_id,
                            "received_at": stamp(),
                            "rights": dict(rights),
                            "identities": identities,
                            "securities": securities,
                            "quote_server_connected": True,
                        }
                        encoded = json.dumps(result, default=str) + "\n"
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(encoded)
                    temporary.replace(path)
                    if time.monotonic() - last_archive >= 5:
                        # One file per day, capped, so a 10-day run cannot fill the disk.
                        target = archive_path(path.parent, datetime.now(timezone.utc))
                        if target is not None:
                            if not target.exists() or target.stat().st_size < ARCHIVE_DAILY_BYTES:
                                with gzip.open(target, "at") as archive:
                                    archive.write(encoded)
                            elif target != capped:
                                print("Moomoo raw archive cap reached:", target, flush=True)
                                capped = target
                        last_archive = time.monotonic()
                        from datetime import timedelta
                        from zoneinfo import ZoneInfo

                        with lock:
                            bar_rows = list(bars.values())
                        if bar_rows:
                            bar_path = path.parent / "live-bars.csv"
                            temporary_bars = bar_path.with_suffix(".tmp")
                            with temporary_bars.open("w") as output:
                                writer = csv.DictWriter(
                                    output,
                                    fieldnames=[
                                        "timestamp",
                                        "symbol",
                                        "open",
                                        "high",
                                        "low",
                                        "close",
                                        "volume",
                                    ],
                                )
                                writer.writeheader()
                                for row in sorted(bar_rows, key=lambda r: (r["code"], r["time_key"])):
                                    at = datetime.fromisoformat(row["time_key"]).replace(
                                        tzinfo=ZoneInfo("America/New_York")
                                    ) + timedelta(minutes=1)
                                    writer.writerow(
                                        {
                                            "timestamp": at.isoformat(),
                                            "symbol": row["code"][3:],
                                            **{k: row[k] for k in ["open", "high", "low", "close", "volume"]},
                                        }
                                    )
                            temporary_bars.replace(bar_path)
                    time.sleep(1)
            except Exception as exc:
                print("Quote-only collector retry:", type(exc).__name__, flush=True)
                time.sleep(10)
            finally:
                if ctx is not None:
                    ctx.close()


if __name__ == "__main__":
    main()

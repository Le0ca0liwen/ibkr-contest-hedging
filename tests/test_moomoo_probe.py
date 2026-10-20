from types import SimpleNamespace

import pandas as pd
import pytest

from ops.probe_moomoo import probe


def test_moomoo_probe_uses_only_quote_context_and_preserves_failures():
    calls = []

    class QuoteContext:
        def __init__(self, **kwargs):
            assert kwargs == {"host": "127.0.0.1", "port": 11111}
            calls.append("quote_context")

        def subscribe(self, codes, types, subscribe_push):
            assert types == ["quote", "book"]
            assert subscribe_push is False
            return (0, "") if codes == ["US.BA"] else (1, "No OPRA right")

        def get_stock_quote(self, codes):
            return 0, pd.DataFrame([{"code": codes[0], "data_time": "09:30:00"}])

        def get_order_book(self, code, num):
            assert code == "US.BA" and num == 1
            return 0, {"Bid": [(195, 100, 1)], "Ask": [(196, 100, 1)]}

        def close(self):
            calls.append("close")

    # No trade constructors exist in this SDK stub. Using one would fail.
    sdk = SimpleNamespace(
        OpenQuoteContext=QuoteContext,
        RET_OK=0,
        SubType=SimpleNamespace(QUOTE="quote", ORDER_BOOK="book"),
    )
    result = probe(sdk, ["US.BA", "US.BA261016C195000"])
    assert calls == ["quote_context", "close"]
    assert result["execution_enabled"] is False
    assert result["live_entitlement_verified"] is False
    assert result["securities"]["US.BA"]["quote"][0]["data_time"] == "09:30:00"
    assert result["securities"]["US.BA261016C195000"]["error"] == "No OPRA right"


def test_probe_rejects_nonmarket_codes_before_connecting():
    with pytest.raises(ValueError):
        probe(None, ["place_order"])


def test_collector_applies_quote_right_downgrades_from_sdk_tuple():
    import importlib.util
    import threading
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("record_moomoo", "ops/record_moomoo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Base:
        def on_recv_rsp(self, rsp):
            return rsp

    sdk = SimpleNamespace(
        StockQuoteHandlerBase=Base,
        OrderBookHandlerBase=Base,
        SysNotifyHandlerBase=Base,
        CurKlineHandlerBase=Base,
        RET_OK=0,
        SysNotifyType=SimpleNamespace(QOT_RIGHT="QOT_RIGHT"),
    )
    rights = {"us_qot_right": "LV3", "us_option_qot_right": "LV1"}
    notify = module.make_handlers(sdk, threading.Lock(), {}, rights, lambda: "now")[2]
    # moomoo-api 10.11 returns (notify_type, sub_type, payload), not a dict.
    notify.on_recv_rsp((0, ("QOT_RIGHT", None, {"us_option_qot_right": "NO", "other": "x"})))
    assert rights == {"us_qot_right": "LV3", "us_option_qot_right": "NO"}


def test_raw_archive_only_during_the_new_york_session(tmp_path):
    import importlib.util
    from datetime import datetime, timezone

    spec = importlib.util.spec_from_file_location("record_moomoo", "ops/record_moomoo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 19:30 UTC = 15:30 New York (EDT): archived under the New York date.
    assert module.archive_path(tmp_path, datetime(2026, 10, 22, 19, 30, tzinfo=timezone.utc)).name == (
        "raw-20261022.jsonl.gz"
    )
    assert (
        module.archive_path(tmp_path, datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)) is None
    )  # overnight
    assert (
        module.archive_path(tmp_path, datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)) is None
    )  # Saturday

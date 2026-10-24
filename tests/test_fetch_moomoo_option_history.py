from datetime import date
from types import SimpleNamespace

import pandas as pd

from ops.fetch_moomoo_option_history import fetch

SDK = SimpleNamespace(RET_OK=0, KLType=SimpleNamespace(K_1M="K_1M"), AuType=SimpleNamespace(NONE="NONE"))


class Ctx:
    def __init__(self, remaining):
        self.remaining, self.klines = remaining, []

    def get_history_kl_quota(self, get_detail=False):
        return (0, (10, self.remaining, None)) if self.remaining is not None else (-1, "error")

    def get_market_snapshot(self, codes):
        return 0, pd.DataFrame([{"code": codes[0], "last_price": 100.0}])

    def get_option_chain(self, code, start, end):
        rows = [
            {"code": f"{code}C{k}", "strike_price": k, "option_type": "CALL"} for k in (80, 95, 100, 105, 130)
        ]
        return 0, pd.DataFrame(rows)

    def request_history_kline(self, code, **kw):
        self.klines.append(code)
        data = pd.DataFrame(
            [
                {
                    "time_key": "2026-10-22 15:59:00",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1.1,
                    "volume": 3,
                },
                {
                    "time_key": "2026-10-22 10:00:00",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1.0,
                    "volume": 3,
                },
            ]
        )
        return 0, data, None


def test_fetch_keeps_quota_in_reserve_and_takes_the_nearest_strikes_first():
    ctx = Ctx(remaining=42)
    result = fetch(ctx, SDK, ["X"], ["2026-11-20"], [date(2026, 10, 22)], 0.12, 40, sleep=lambda s: None)
    assert ctx.klines[0] == "US.XC100" and len(ctx.klines) == 2  # 2 of quota spare; 80/130 outside 12%
    assert result["skipped"] == 1 and [b["close"] for b in result["options"][0]["bars"]] == [
        1.1
    ]  # 15:30-16:15 only
    assert (
        fetch(
            Ctx(remaining=None),
            SDK,
            ["X"],
            ["2026-11-20"],
            [date(2026, 10, 22)],
            0.12,
            40,
            sleep=lambda s: None,
        )["options"]
        == []
    )

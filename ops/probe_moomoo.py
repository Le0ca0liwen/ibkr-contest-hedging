"""Standalone market-data diagnostic. No IBKR connection or trading APIs.

Run in the isolated SDK environment described in docs/MOOMOO_MARKET_DATA.md.
Results are diagnostic inputs, never executable Quote objects.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def probe(sdk, codes, port=11111, context=None):
    if not codes or any(not re.fullmatch(r"US\.[A-Z0-9.]+", code) for code in codes):
        raise ValueError("Provide explicit US stock/option market-data codes")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local OpenD port")
    own_context = context is None
    context = context or sdk.OpenQuoteContext(host="127.0.0.1", port=port)
    result = {
        "provider": "moomoo",
        "purpose": "market_data_only",
        "execution_enabled": False,
        "live_entitlement_verified": False,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "securities": {},
        "warning": "Pull receipt time does not establish quote freshness or live entitlement.",
    }
    try:
        # Quote entitlements only (never BASIC, WEBKEY, trade or account fields).
        if hasattr(context, "get_user_info"):
            status, info = context.get_user_info([sdk.UserInfoField.QOTRIGHT])
            if status == sdk.RET_OK:
                result["rights"] = {k: info.get(k) for k in ("us_qot_right", "us_option_qot_right")}
        # Subscribe independently so one missing option right does not conceal stock coverage.
        for code in dict.fromkeys(codes):
            item = {}
            result["securities"][code] = item
            status, message = context.subscribe(
                [code], [sdk.SubType.QUOTE, sdk.SubType.ORDER_BOOK], subscribe_push=False
            )
            item["subscription_ok"] = status == sdk.RET_OK
            if status != sdk.RET_OK:
                item["error"] = str(message)
                continue
            status, data = context.get_stock_quote([code])
            if status == sdk.RET_OK:
                # Preserve provider timestamps, IV percentage units, and raw Greeks.
                # Do not pretend last-trade time is the timestamp of bid/ask.
                columns = [
                    name
                    for name in (
                        "code",
                        "data_date",
                        "data_time",
                        "last_price",
                        "strike_price",
                        "contract_size",
                        "contract_multiplier",
                        "implied_volatility",
                        "delta",
                        "gamma",
                        "vega",
                        "theta",
                        "sec_status",
                    )
                    if name in data.columns
                ]
                item["quote"] = json.loads(data[columns].to_json(orient="records"))
            else:
                item["quote_error"] = str(data)
            status, data = context.get_order_book(code, num=1)
            if status == sdk.RET_OK:
                item["order_book"] = data
            else:
                item["book_error"] = str(data)
        return result
    finally:
        if own_context:
            context.close()


def main():
    parser = argparse.ArgumentParser(description="Moomoo quotes only; never submits orders")
    parser.add_argument("--code", action="append", required=True)
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--output", default="reports/moomoo-market-data-probe.json")
    args = parser.parse_args()
    import moomoo

    result = probe(moomoo, args.code, args.port)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

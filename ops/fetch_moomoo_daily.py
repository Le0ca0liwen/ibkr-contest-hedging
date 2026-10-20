"""Download prior completed daily bars through quote-only OpenD; never trades."""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", default="data/moomoo/daily-stocks.csv")
    args = parser.parse_args()
    import moomoo as sdk
    import pandas as pd

    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
    frames = []
    try:
        for symbol in sorted(set(args.symbols)):
            ret, data, next_page = ctx.request_history_kline(
                "US." + symbol,
                start=str(args.as_of - timedelta(days=240)),
                end=str(args.as_of - timedelta(days=1)),
                ktype=sdk.KLType.K_DAY,
                autype=sdk.AuType.QFQ,
                max_count=1000,
            )
            if ret != sdk.RET_OK or next_page is not None:
                raise ValueError(f"Historical coverage unavailable or incomplete: {symbol}")
            frame = data[["time_key", "close"]].rename(columns={"time_key": "date"})
            frame["symbol"] = symbol
            frames.append(frame)
        joined = pd.concat(frames, ignore_index=True)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: the running service reloads this file when it changes.
        temporary = path.with_suffix(".tmp")
        joined.to_csv(temporary, index=False)
        temporary.replace(path)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "provider": "moomoo",
                    "purpose": "market_data_only",
                    "execution_enabled": False,
                    "as_of": str(args.as_of),
                    "adjustment": "QFQ",
                    "symbols": sorted(set(args.symbols)),
                    "rows": len(joined),
                }
            )
        )
        print(f"Archived {len(joined)} prior completed daily observations")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()

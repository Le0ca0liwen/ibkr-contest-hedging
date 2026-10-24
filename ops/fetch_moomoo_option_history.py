"""Quote-only Moomoo 1-minute option bars around past closes, to tune the option-mark model.

    .runtime/moomoo-env/bin/python ops/fetch_moomoo_option_history.py \
        --underlyings SPCX RKLB SPY --expiries 2026-11-20 2026-11-27 \
        --days 2026-10-21 2026-10-22 2026-10-23 --moneyness 0.12 --quota-cap 40

Bars are trades (open/high/low/close/volume), not quotes: implied-vol anchors where the app has no
closing book. Each new code uses Moomoo's 30-day historical-candlestick quota, which the app's daily
price refresh also needs, so the script asks for the quota first, keeps --quota-cap codes in reserve
and fetches the strikes nearest the money first. Never trades.
"""

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

WINDOW = ("15:30:00", "16:15:00")


def quota(ctx):
    """(used, remaining) of the historical candlestick quota, or (None, None)."""
    ret, data = ctx.get_history_kl_quota(get_detail=False)
    if ret != 0:
        return None, None
    used, remaining = data[:2]
    return int(used), int(remaining)


def chain_codes(ctx, sdk, underlyings, expiries, moneyness, sleep=time.sleep):
    """[(distance from the money, code, underlying, expiry, strike, right)] nearest-the-money first."""
    rows = []
    for symbol in underlyings:
        ret, snap = ctx.get_market_snapshot([f"US.{symbol}"])
        if ret != sdk.RET_OK or snap.empty:
            continue
        spot = float(snap.iloc[0]["last_price"])
        for expiry in expiries:
            ret, chain = ctx.get_option_chain(code=f"US.{symbol}", start=expiry, end=expiry)
            sleep(3.0)
            if ret != sdk.RET_OK:
                continue
            for _, row in chain.iterrows():
                strike = float(row["strike_price"])
                if abs(strike / spot - 1) <= moneyness:
                    right = "C" if str(row["option_type"]).upper().startswith("C") else "P"
                    rows.append((abs(strike / spot - 1), row["code"], symbol, expiry, strike, right))
    return sorted(rows)


def bars(ctx, sdk, code, days, sleep=time.sleep):
    """1-minute bars of `code` on `days` inside WINDOW (New York time keys)."""
    out, page = [], None
    while True:
        ret, data, page = ctx.request_history_kline(
            code,
            start=str(min(days)),
            end=str(max(days)),
            ktype=sdk.KLType.K_1M,
            autype=sdk.AuType.NONE,
            max_count=1000,
            page_req_key=page,
        )
        sleep(0.75)
        if ret != sdk.RET_OK:
            return out, str(data)
        for _, row in data.iterrows():
            key = str(row["time_key"])
            if key[:10] in {str(d) for d in days} and WINDOW[0] <= key[11:19] <= WINDOW[1]:
                out.append({k: row[k] for k in ("time_key", "open", "high", "low", "close", "volume")})
        if page is None:
            return out, None


def fetch(ctx, sdk, underlyings, expiries, days, moneyness, quota_cap, sleep=time.sleep):
    used, remaining = quota(ctx)
    budget = 0 if remaining is None else max(0, remaining - quota_cap)
    codes = chain_codes(ctx, sdk, underlyings, expiries, moneyness, sleep)
    result = {
        "at": datetime.now(timezone.utc).isoformat(),
        "quota": {"used": used, "remaining": remaining, "reserved": quota_cap},
        "options": [],
        "errors": [],
        "skipped": max(0, len(codes) - budget),
    }
    for _, code, symbol, expiry, strike, right in codes[:budget]:
        found, error = bars(ctx, sdk, code, days, sleep)
        if error:
            result["errors"].append(f"{code}: {error}")
        result["options"].append(
            {
                "code": code,
                "underlying": symbol,
                "expiry": expiry,
                "strike": strike,
                "right": right,
                "bars": found,
            }
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Moomoo option minute bars around past closes; never trades")
    parser.add_argument("--underlyings", nargs="+", default=["SPCX", "RKLB", "SPY"])
    parser.add_argument("--expiries", nargs="+", default=["2026-11-20", "2026-11-27"])
    parser.add_argument("--days", nargs="+", type=date.fromisoformat, required=True)
    parser.add_argument("--moneyness", type=float, default=0.12)
    parser.add_argument("--quota-cap", type=int, default=40, help="codes of quota left untouched")
    parser.add_argument("--output", default="data/calibration/option-bars.json")
    args = parser.parse_args(argv)
    import moomoo as sdk

    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        result = fetch(ctx, sdk, args.underlyings, args.expiries, args.days, args.moneyness, args.quota_cap)
    finally:
        ctx.close()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, default=str) + "\n")
    with_bars = sum(bool(o["bars"]) for o in result["options"])
    print(
        f"{len(result['options'])} codes ({with_bars} with bars, {result['skipped']} skipped for quota); "
        f"quota {result['quota']}; {len(result['errors'])} errors; wrote {path}"
    )


if __name__ == "__main__":
    main()

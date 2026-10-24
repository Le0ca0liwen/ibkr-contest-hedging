"""Quote-only option chain snapshot through OpenD; never trades or reads an account.

    .runtime/moomoo-env/bin/python ops/fetch_moomoo_chain.py --underlyings SPCX RKLB SPY \\
        --expiries 2026-11-20 2026-11-27 --moneyness 0.3 --output data/moomoo/chain.json

Each underlying's snapshot gives its spot; the listed strikes of each expiry within +/- moneyness of
it are then snapshotted in requests of at most 400 codes, one request per 0.6 s (OpenD allows 60
snapshot requests and 10 option chain requests per 30 s).
"""

import argparse
import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CHUNK = 400  # codes per get_market_snapshot request (OpenD limit)
CHAIN_PAUSE = 3.0  # get_option_chain: at most 10 requests per 30 s
RIGHTS = {"CALL": "C", "PUT": "P"}


def _records(frame):
    return json.loads(frame.to_json(orient="records"))


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fetch_chain(
    ctx, sdk, underlyings, expiries, moneyness, pause=0.6, sleep=time.sleep, chain_pause=CHAIN_PAUSE
):
    """{"at", "underlyings": {symbol: {"spot", "bid", "ask"}}, "options": [{"code", "underlying",
    "expiry", "strike", "right", "bid", "ask", "last", "update_time"}], "errors": [...]}.

    Quote requests only (get_market_snapshot, get_option_chain), at least `pause` seconds apart
    (`chain_pause` before an option chain request). A failed request is recorded and skipped."""
    made = []

    def request(name, *args, gap=pause, **kwargs):
        if made:
            sleep(gap)
        made.append(name)
        return getattr(ctx, name)(*args, **kwargs)

    spots, chosen, errors = {}, {}, []
    for symbol in underlyings:
        code = f"US.{symbol}"
        ret, data = request("get_market_snapshot", [code])
        row = (_records(data) or [{}])[0] if ret == sdk.RET_OK else {}
        bid, ask = _number(row.get("bid_price")), _number(row.get("ask_price"))
        spot = _number(row.get("last_price"))
        if not spot or spot <= 0:
            spot = (bid + ask) / 2 if bid and ask and 0 < bid <= ask else None
        if spot is None:
            errors.append(f"No spot for {code}: {data if ret != sdk.RET_OK else 'no price'}")
            continue
        spots[symbol] = {"spot": spot, "bid": bid, "ask": ask}
        for expiry in expiries:
            ret, data = request(
                "get_option_chain", code=code, start=expiry, end=expiry, gap=max(pause, chain_pause)
            )
            if ret != sdk.RET_OK:
                errors.append(f"No option chain for {code} {expiry}: {data}")
                continue
            listed = 0
            for row in _records(data):
                strike, right = _number(row.get("strike_price")), RIGHTS.get(row.get("option_type"))
                if not row.get("code") or not strike or right is None:
                    continue
                listed += 1
                if abs(strike - spot) <= moneyness * spot + 1e-9:
                    chosen[row["code"]] = {
                        "code": row["code"],
                        "underlying": symbol,
                        "expiry": row.get("strike_time") or expiry,
                        "strike": strike,
                        "right": right,
                    }
            if not listed:
                errors.append(f"No options listed for {code} {expiry}")
    codes = list(chosen)
    for start in range(0, len(codes), CHUNK):
        chunk = codes[start : start + CHUNK]
        ret, data = request("get_market_snapshot", chunk)
        if ret != sdk.RET_OK:
            errors.append(f"Option snapshot failed for {len(chunk)} codes: {data}")
            continue
        for row in _records(data):
            option = chosen.get(row.get("code"))
            if option is not None:
                option.update(
                    bid=_number(row.get("bid_price")),
                    ask=_number(row.get("ask_price")),
                    last=_number(row.get("last_price")),
                    update_time=row.get("update_time"),
                )
    options = sorted(
        (o for o in chosen.values() if "bid" in o),
        key=lambda o: (o["underlying"], o["expiry"], o["right"], o["strike"]),
    )
    if len(options) < len(chosen):
        errors.append(f"{len(chosen) - len(options)} option codes returned no snapshot")
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "underlyings": spots,
        "options": options,
        "errors": errors,
    }


def write_json(path, value):
    """Atomic replace: a reader never sees a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=1) + "\n")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Moomoo option chain quotes only; never trades")
    parser.add_argument("--underlyings", nargs="+", default=["SPCX", "RKLB", "SPY"])
    parser.add_argument(
        "--expiries", nargs="+", type=date.fromisoformat, default=[date(2026, 11, 20), date(2026, 11, 27)]
    )
    parser.add_argument("--moneyness", type=float, default=0.3)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not 0 < args.moneyness < 1:
        parser.error("--moneyness is a fraction of spot, between 0 and 1")
    output = args.output or (
        f"data/moomoo/chain-{datetime.now(ZoneInfo('America/New_York')):%Y%m%d-%H%M}.json"
    )
    import moomoo as sdk

    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        result = fetch_chain(
            ctx, sdk, [s.upper() for s in args.underlyings], [str(e) for e in args.expiries], args.moneyness
        )
    finally:
        ctx.close()
    for error in result["errors"]:
        print("Skipped:", error)
    if not result["options"]:
        raise SystemExit(f"No option quotes: {output} not written")  # keeps an earlier good file
    write_json(output, result)
    print(f"{len(result['options'])} option quotes for {', '.join(result['underlyings'])} in {output}")


if __name__ == "__main__":
    main()

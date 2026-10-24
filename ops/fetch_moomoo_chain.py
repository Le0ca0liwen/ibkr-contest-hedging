"""Quote-only option chain snapshot through OpenD; never trades or reads an account.

    .runtime/moomoo-env/bin/python ops/fetch_moomoo_chain.py --underlyings SPCX RKLB SPY \\
        --expiries 2026-11-20 2026-11-27 --moneyness 0.3 --output data/moomoo/chain.json

Each underlying's snapshot gives its spot; the listed strikes of each expiry within +/- moneyness of
it are then snapshotted in requests of at most 400 codes, one request per 0.6 s (OpenD allows 60
snapshot requests and 10 option chain requests per 30 s).

Listing is slow (one option chain request per 3 s) and quoting is fast, so the app lists the codes
minutes before the close (--list-only) and quotes them at 15:59:57 (--codes FILE --at TIME): the
clearing house prices every option from the 4:00 p.m. NBBO.
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


def _requester(ctx, pause, sleep):
    made = []

    def request(name, *args, gap=pause, **kwargs):
        if made:
            sleep(gap)
        made.append(name)
        return getattr(ctx, name)(*args, **kwargs)

    return request


def fetch_chain(
    ctx, sdk, underlyings, expiries, moneyness, pause=0.6, sleep=time.sleep, chain_pause=CHAIN_PAUSE
):
    """{"at", "underlyings": {symbol: {"spot", "bid", "ask"}}, "options": [{"code", "underlying",
    "expiry", "strike", "right", "bid", "ask", "last", "update_time"}], "errors": [...]}.

    Quote requests only (get_market_snapshot, get_option_chain), at least `pause` seconds apart
    (`chain_pause` before an option chain request). A failed request is recorded and skipped."""
    request = _requester(ctx, pause, sleep)  # one pacing across both steps
    listing = list_chain(ctx, sdk, underlyings, expiries, moneyness, pause, sleep, chain_pause, request)
    return snapshot_chain(
        ctx,
        sdk,
        listing,
        pause,
        sleep,
        spots=listing["underlyings"],
        errors=listing["errors"],
        request=request,
    )


def list_chain(
    ctx,
    sdk,
    underlyings,
    expiries,
    moneyness,
    pause=0.6,
    sleep=time.sleep,
    chain_pause=CHAIN_PAUSE,
    request=None,
):
    """The listed codes within +/- moneyness of each spot, without their quotes ({"underlyings",
    "options", "errors"}): slow (one option chain request per 3 s), so taken minutes before the
    close and snapshotted with snapshot_chain at the close."""
    request = request or _requester(ctx, pause, sleep)
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
    return {"underlyings": spots, "options": list(chosen.values()), "errors": errors}


def snapshot_chain(ctx, sdk, listing, pause=0.6, sleep=time.sleep, spots=None, errors=None, request=None):
    """Quotes for a list_chain listing: one snapshot of the underlyings (unless `spots` are given)
    and of the option codes in requests of at most CHUNK; a few seconds for a few hundred codes."""
    request = request or _requester(ctx, pause, sleep)
    errors = list(errors or [])
    chosen = {o["code"]: dict(o) for o in listing["options"]}
    if spots is None:
        spots = {}
        symbols = sorted(listing["underlyings"])
        ret, data = request("get_market_snapshot", [f"US.{s}" for s in symbols])
        rows = {r.get("code"): r for r in _records(data)} if ret == sdk.RET_OK else {}
        for symbol in symbols:
            row = rows.get(f"US.{symbol}", {})
            bid, ask, last = (
                _number(row.get("bid_price")),
                _number(row.get("ask_price")),
                _number(row.get("last_price")),
            )
            spot = (
                last if last and last > 0 else ((bid + ask) / 2 if bid and ask and 0 < bid <= ask else None)
            )
            spots[symbol] = {"spot": spot or listing["underlyings"][symbol]["spot"], "bid": bid, "ask": ask}
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


def wait_until(at, sleep=time.sleep, clock=time.time):
    """Sleep until the epoch time `at` (no wait once it has passed)."""
    if at is not None and at > clock():
        sleep(at - clock())


def only_underlyings(listing, underlyings):
    """The listing restricted to `underlyings` (all of it when None)."""
    if not underlyings:
        return listing
    keep = set(underlyings)
    return {
        **listing,
        "underlyings": {s: v for s, v in listing["underlyings"].items() if s in keep},
        "options": [o for o in listing["options"] if o["underlying"] in keep],
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
    parser.add_argument(
        "--list-only", action="store_true", help="write the codes only (minutes before the close)"
    )
    parser.add_argument("--codes", help="snapshot the codes of an earlier --list-only file (at the close)")
    parser.add_argument(
        "--at",
        type=datetime.fromisoformat,
        help="quote the codes at this time (ISO, with offset): connect and list first, then wait",
    )
    args = parser.parse_args(argv)
    if not 0 < args.moneyness < 1:
        parser.error("--moneyness is a fraction of spot, between 0 and 1")
    output = args.output or (
        f"data/moomoo/chain-{datetime.now(ZoneInfo('America/New_York')):%Y%m%d-%H%M}.json"
    )
    import moomoo as sdk

    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
    underlyings, expiries = [s.upper() for s in args.underlyings], [str(e) for e in args.expiries]
    try:
        if args.list_only:
            listing = list_chain(ctx, sdk, underlyings, expiries, args.moneyness)
            if not listing["options"]:
                raise SystemExit(f"No option codes listed: {output} not written")
            write_json(output, listing)
            print(f"{len(listing['options'])} option codes listed in {output}")
            return
        if args.codes or args.at:
            if args.codes:
                listing = only_underlyings(json.loads(Path(args.codes).read_text()), underlyings)
            else:
                listing = list_chain(ctx, sdk, underlyings, expiries, args.moneyness)
            wait_until(args.at.timestamp() if args.at else None)
            # The spots are quoted again with the options, not taken from the listing.
            result = snapshot_chain(ctx, sdk, listing, errors=listing.get("errors"))
        else:
            result = fetch_chain(ctx, sdk, underlyings, expiries, args.moneyness)
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

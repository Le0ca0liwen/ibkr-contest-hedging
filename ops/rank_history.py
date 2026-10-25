"""Past days' OCC option prices for whole chains, from Moomoo's option ranking. Read-only: a quote
context only, never a trade context.

    .runtime/moomoo-env/bin/python ops/rank_history.py --start 2026-09-15 --end 2026-10-23 \\
        [--underlyings SPY RKLB SPCX BA QQQ IWM] [--truth-chain FILE=DAY ...] \\
        [--output data/calibration/rank-history.json]

get_option_rank(trading_date=D) lists each option of an underlying on day D with its last trade
(option_price) and its change against the previous close (change_ratio, percent). An option's
previous close is OCC's price of the day before (Moomoo's prev_close_price equals the statements),
so option_price / (1 + change_ratio / 100) recovers OCC's price of the trading day before D: 17 of 17
statement legs, Oct 7-22, to the 3-decimal rounding of change_ratio. That holds for a contract that
traded on D; the others are kept apart ("untraded") until --truth-chain shows what they carry.

Each underlying and day is asked by volume in four parts (calls and puts, in and out of the money),
1,000 contracts at most each (OpenD pages 200 at a time, five pages). A date OpenD does not hold comes
back silently as the latest day, so a reply for another day is dropped. The ranking's bid/ask are the
session's last book (16:10-16:15), not the 16:00 one, so they are not kept: closing books come from
IBKR (ops/fetch_ibkr_closing_books.py). Untraded contracts are kept only on days a --truth-chain
checks, to keep the file small (a month of six chains is about 500,000 rows).

The output has ops/mark_backtest.py's --dataset format: under each day D an "option_chain" whose
prev_close prices are the previous weekday's truth. "check" compares the recovered prices with the
statements' held legs and with each --truth-chain file (a chain snapshot's prev_close for DAY).
"""

import argparse
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PAGE, PAGES = 200, 5  # OpenD: 200 rows a page, five pages at most
PAUSE = 0.6  # 60 ranking requests per 30 s; only a paged query's first request counts
PARTS = [(right, money) for right in (1, 2) for money in (0, 1)]  # (call/put, out/in the money)
CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})[CP]\d+$")


def previous_weekday(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekdays(start, end):
    day, found = start, []
    while day <= end:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def implied_previous(row):
    """OCC's price of the trading day before, from the last trade and its change ratio (percent)."""
    price, change = _number(row.get("option_price")), _number(row.get("change_ratio"))
    if price is None or change is None or price <= 0 or change <= -100:
        return None
    return price / (1 + change / 100)


def rank_part(ctx, sdk, make_filter, symbol, day, right, money, sleep=time.sleep):
    """(rows, error) of one part of one underlying's ranking on `day`, by volume."""
    filters = [
        make_filter(sdk.OptionRankIndicatorType.OWNER_LIST, security_list=[f"US.{symbol}"]),
        make_filter(sdk.OptionRankIndicatorType.OPTION_TYPE, value_list=[right]),
        make_filter(sdk.OptionRankIndicatorType.IN_THE_MONEY, value_list=[money]),
    ]
    rows, page = [], None
    for number in range(PAGES):
        sleep(PAUSE if number == 0 else 0.2)
        try:
            ret, data, *rest = ctx.get_option_rank(
                sdk.OptionMarket.US_SECURITY,
                sdk.OptionRankType.VOLUME,
                count=PAGE,
                trading_date=str(day),
                page=page,
                filter_list=filters,
            )
        except Exception as exc:  # one failed part never stops the run
            return rows, f"{type(exc).__name__}: {exc}"
        if ret != sdk.RET_OK:
            return rows, str(data)
        rows += json.loads(data.to_json(orient="records"))
        page = rest[0] if rest else None
        if not page:
            break
    return rows, None


def rank_day(ctx, sdk, make_filter, symbol, day, sleep=time.sleep):
    """({code: row}, errors) of every part of one underlying on `day`; replies for another day are
    dropped (OpenD answers a date it does not hold with the latest one, without an error)."""
    found, errors = {}, []
    for right, money in PARTS:
        rows, error = rank_part(ctx, sdk, make_filter, symbol, day, right, money, sleep)
        if error:
            errors.append(f"{symbol} {day} part {right}/{money}: {error}")
        other = {str(r.get("trading_date"))[:10] for r in rows} - {str(day)}
        if other:
            errors.append(f"{symbol} {day}: OpenD answered for {sorted(other)}; dropped")
            return {}, errors
        found.update({r["code"]: r for r in rows if r.get("code")})
    return found, errors


def entry(row):
    previous = implied_previous(row)
    return {
        "code": row["code"],
        "prev_close": None if previous is None else round(previous, 6),
        "volume": row.get("volume"),
    }


def build(ranked, checked_days=()):
    """The --dataset days from {day: {code: row}}; untraded contracts only on `checked_days`."""
    days = {}
    for day, rows in sorted(ranked.items()):
        traded = [entry(r) for r in rows.values() if (r.get("volume") or 0) > 0]
        untraded = [entry(r) for r in rows.values() if not (r.get("volume") or 0) > 0]
        days[str(day)] = {
            "option_chain": [
                {
                    "source": f"Moomoo option ranking {day}: prev_close = OCC price of {previous_weekday(day)}",
                    "options": [o for o in traded if o["prev_close"] is not None],
                    "untraded": untraded if day in checked_days else [],
                }
            ]
        }
    return days


def wanted(days, symbols, per_day, today):
    """{truth day: [codes]} for ops/fetch_ibkr_closing_books.py --wanted: per day and underlying, the
    `per_day` most traded contracts with a recovered price that IBKR can still serve (not expired)."""
    found = {}
    for day, value in sorted(days.items()):
        truth_day = str(previous_weekday(date.fromisoformat(day)))
        for symbol in symbols:
            rows = []
            for o in value["option_chain"][0]["options"]:
                match = CODE.match(o["code"])
                if match and match[1] == symbol and f"20{match[2]}" >= f"{today:%Y%m%d}":
                    rows.append(o)
            rows.sort(key=lambda o: -(o["volume"] or 0))
            found.setdefault(truth_day, []).extend(o["code"] for o in rows[:per_day])
    return found


def statement_truth(folder):
    """{day: {code: Close Price}} of every held option on a saved statement."""
    truth = {}
    for path in sorted(Path(folder).glob("*.json")) if folder else []:
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") == "OPT" and p.get("mark") not in (None, ""):
                code = "US.{}{}{}{}".format(
                    p["symbol"].split()[0],
                    p["expiry"].replace("-", "")[2:],
                    p["put_call"],
                    round(float(p["strike"]) * 1000),
                )
                truth.setdefault(data["date"], {})[code] = float(p["mark"])
    return truth


def check(days, truth):
    """{"traded"/"untraded": {"equal", "compared"}, "misses": [...]}: recovered prices against `truth`
    ({day: {code: OCC price}}), equal within the change ratio's rounding (and a 0.0001 floor)."""
    result = {"traded": [0, 0], "untraded": [0, 0], "misses": []}
    for day, entry_ in days.items():
        known = truth.get(str(previous_weekday(date.fromisoformat(day))), {})
        chain = entry_["option_chain"][0]
        for kind, rows in (("traded", chain["options"]), ("untraded", chain["untraded"])):
            for o in rows:
                price = known.get(o["code"])
                if price is None or o["prev_close"] is None:
                    continue
                hit = abs(o["prev_close"] - price) <= max(1e-4, 2e-5 * price)
                result[kind][0] += hit
                result[kind][1] += 1
                if not hit and len(result["misses"]) < 40:
                    result["misses"].append(
                        [day, o["code"], kind, price, round(o["prev_close"], 4), o["volume"]]
                    )
    return {
        "traded": dict(zip(("equal", "compared"), result["traded"], strict=True)),
        "untraded": dict(zip(("equal", "compared"), result["untraded"], strict=True)),
        "misses": result["misses"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Past OCC option prices from Moomoo's option ranking")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 15))
    parser.add_argument(
        "--end", type=date.fromisoformat, default=datetime.now(ZoneInfo("America/New_York")).date()
    )
    parser.add_argument("--underlyings", nargs="+", default=["SPY", "RKLB", "SPCX", "BA", "QQQ", "IWM"])
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument(
        "--truth-chain",
        nargs="*",
        default=[],
        metavar="FILE=DAY",
        help="a chain file whose prev_close is DAY's",
    )
    parser.add_argument("--output", default=str(ROOT / "data/calibration/rank-history.json"))
    parser.add_argument("--wanted-output", help="also write {truth day: [codes]} for the IBKR books fetch")
    parser.add_argument("--wanted-underlyings", nargs="*", default=["RKLB", "SPCX"])
    parser.add_argument("--wanted-per-day", type=int, default=40)
    args = parser.parse_args(argv)
    import moomoo as sdk
    from moomoo.quote.quote_option_event_info import OptionRankFilter  # not exported by moomoo

    truth = statement_truth(args.statements)
    for item in args.truth_chain:
        path, day = item.rsplit("=", 1)
        for o in json.loads(Path(path).read_text()).get("options", []):
            if _number(o.get("prev_close")):
                truth.setdefault(day, {}).setdefault(o["code"], float(o["prev_close"]))
    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)  # quote context only
    ranked, errors = {}, []
    try:
        for day in weekdays(args.start, args.end):
            for symbol in args.underlyings:
                rows, problems = rank_day(ctx, sdk, OptionRankFilter, symbol, day)
                errors += problems
                ranked.setdefault(day, {}).update(rows)
            print(f"{day}: {len(ranked.get(day, {}))} contracts ranked", flush=True)
    finally:
        ctx.close()
    checked = {
        d for d in ranked if str(previous_weekday(d)) in {i.rsplit("=", 1)[1] for i in args.truth_chain}
    }
    days = {
        day: value for day, value in build(ranked, checked).items() if value["option_chain"][0]["options"]
    }
    result = {"exported_at": datetime.now(ZoneInfo("America/New_York")).isoformat(), "days": days}
    result["check"] = check(days, truth)
    result["errors"] = errors
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(result) + "\n")
    temporary.replace(output)
    for day, value in days.items():
        chain = value["option_chain"][0]
        print(f"{day}: {len(chain['options'])} traded prices, {len(chain['untraded'])} untraded")
    print(
        "check:",
        json.dumps(result["check"]["traded"]),
        "traded,",
        json.dumps(result["check"]["untraded"]),
        "untraded",
    )
    for miss in result["check"]["misses"][:10]:
        print("   differs:", miss)
    print(f"{len(errors)} errors; wrote {output}")
    for error in errors[:10]:
        print("  ", error)
    if args.wanted_output:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        lists = wanted(days, args.wanted_underlyings, args.wanted_per_day, today)
        Path(args.wanted_output).write_text(json.dumps(lists, indent=1) + "\n")
        print(f"{sum(map(len, lists.values()))} codes over {len(lists)} days wanted in {args.wanted_output}")


if __name__ == "__main__":
    main()

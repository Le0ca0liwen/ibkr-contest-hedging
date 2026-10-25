"""Each option's 16:00 bid and ask on past days from IBKR's historical data, to tune the option-mark
model (hedge/occ_marks.py) on every October day. Read-only: its own API connection (the app's
client id + 300), historical requests only; never trades.

    .venv/bin/python ops/fetch_ibkr_closing_books.py --config config/local.contest.yaml \\
        --chains data/calibration/chain-prevclose-20261023-2209.json --chain-days 2026-10-22 \\
        [--statements data/calibration/statements] [--moneyness 0.3]

--close 16:15:00 asks for the minute ending then instead (SPY-style options trade until 16:15), into
its own --output and with its own --client-offset beside a running fetch, to test whether OCC prices
those classes from their later book; --symbols keeps only those underlyings.

Contracts: every code of each chain file (ops/fetch_moomoo_chain.py) for each --chain-days day, and
every option on each saved statement for its own day with its chain's strikes nearby (--ladder
strikes each side), so a past day's held legs come with enough of their chain to rebuild them.
For each contract and day it asks for the 15:59 one-minute BID and ASK bars (their closes: the
quotes standing at 16:00, the NBBO the clearing house prices from) and, per underlying, the same
for the shares. One-minute bars are not IBKR's "small bars", so the 60-per-10-minutes limit does
not apply; --concurrency contracts are asked at once, at most --rate requests a second. The statement
legs come first, then the chains nearest the money first, so a partial run is still useful. Output: a JSON list of {"day", "code", "bid",
"ask"} (the format ops/mark_backtest.py --chains reads), written as it goes, so a re-run resumes.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_OFFSET = 300  # the app uses its client id and +100; the overnight marks script used 98
CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")
NO_DATA = (162, 200, 354, 10090, 10091, 10167)  # no bars / no contract / not subscribed


def parse(code):
    match = CODE.match(code)
    if not match:
        return None
    symbol, ymd, right, strike = match.groups()
    return symbol, "20" + ymd, right, int(strike) / 1000


def code_of(symbol, expiry, right, strike):
    return f"US.{symbol}{expiry[2:]}{right}{round(float(strike) * 1000)}"


def listed_strikes(paths):
    """{(symbol, YYYYMMDD expiry): sorted listed strikes} from chain files."""
    listed = {}
    for path in paths:
        for o in json.loads(Path(path).read_text()).get("options", []):
            parsed = parse(o["code"])
            if parsed:
                listed.setdefault((parsed[0], parsed[1]), set()).add(parsed[3])
    return {key: sorted(strikes) for key, strikes in listed.items()}


def statement_wants(folder, ladder, step_of, listed=None):
    """{day: {codes}}: each statement's options and `ladder` strikes each side of each, calls and puts:
    the expiry's listed strikes when a chain file lists them (RKLB Nov 20 lists every dollar from 48,
    so a 2.5 step asked for strikes that do not exist), else steps of step_of(symbol, strike)."""
    wanted = {}
    for path in sorted(Path(folder).glob("*.json")) if folder else []:
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") != "OPT":
                continue
            symbol, expiry = p["symbol"].split()[0], p["expiry"].replace("-", "")
            strike = float(p["strike"])
            strikes = (listed or {}).get((symbol, expiry))
            if strikes:
                near = [k for k in strikes if k < strike][-ladder:] + [k for k in strikes if k > strike][
                    :ladder
                ]
            else:
                step = step_of(symbol, strike)
                near = [strike + k * step for k in range(-ladder, ladder + 1) if k]
            for k in [strike, *near]:
                for right in "CP":
                    wanted.setdefault(data["date"], set()).add(code_of(symbol, expiry, right, k))
    return wanted


def strike_step(symbol, strike):
    """Listed strike spacing near `strike` (1 for SPY, 2.5 for most mid-priced shares, 5 above 200)."""
    if symbol in ("SPY", "QQQ", "IWM", "DIA"):
        return 1.0
    return 1.0 if strike < 25 else 2.5 if strike < 200 else 5.0


def chain_wants(paths, days, moneyness, symbols=None):
    """{day: [codes]} of every chain file's codes within `moneyness` of its spot (of `symbols`
    only, when given), for every day, nearest the money first."""
    distance = {}
    for path in paths:
        chain = json.loads(Path(path).read_text())
        spots = {s: (u or {}).get("spot") for s, u in (chain.get("underlyings") or {}).items()}
        for o in chain.get("options", []):
            spot = spots.get(o["underlying"])
            if symbols and o["underlying"] not in symbols:
                continue
            if spot and abs(o["strike"] / spot - 1) <= moneyness:
                distance[o["code"]] = min(abs(o["strike"] / spot - 1), distance.get(o["code"], 1.0))
    codes = sorted(distance, key=lambda c: (distance[c], c))
    return {str(day): list(codes) for day in days}


def plan(first, then, done, today):
    """[(day, code)] still to fetch, in order: `first` (the statement legs and their neighbours),
    then `then` (whole chains, nearest the money first, the days side by side), each day's shares
    before its options, no
    option on a day after its expiry; options expired by `today` (YYYYMMDD) last, as IBKR mostly has
    no history left for them."""
    todo, later, seen = [], [], set(done)
    for wanted in (first, then):
        batch = []
        for day, codes in sorted(wanted.items()):
            ordered = sorted(codes) if isinstance(codes, set) else codes
            live = [c for c in ordered if parse(c) and parse(c)[1] >= day.replace("-", "")]
            shares = [f"US.{s}" for s in sorted({parse(c)[0] for c in live})]
            batch += [(-1, day, code) for code in shares] + [(i, day, code) for i, code in enumerate(live)]
        if wanted is then:  # the days' chains side by side, nearest the money first
            batch.sort(key=lambda item: (item[0], item[1]))
        for _, day, code in batch:
            if (day, code) not in seen:
                seen.add((day, code))
                expired = parse(code) is not None and parse(code)[1] < today
                (later if expired else todo).append((day, code))
    return todo + later


async def fetch(
    ib, contracts, todo, rate, write, sleep=asyncio.sleep, concurrency=8, timeout=10, close="16:00:00"
):
    """Ask for each (day, code)'s BID and ASK bars of the minute ending at `close` (ET; after 16:00
    outside regular hours), `concurrency` contracts at a time and at
    most `rate` requests starting a second (IBKR answers each in about 2.5 s: one at a time made 12
    books a minute) and `timeout` s each (an expired contract may never answer); no ASK request
    after an empty BID. write({"day", "code", "bid", "ask"}) per success. Returns {key: reason} of
    failures."""
    failures, gap, regular = {}, 1.0 / rate, close <= "16:00:00"
    slots, pace = asyncio.Semaphore(concurrency), asyncio.Lock()

    async def ask(contract, end, side, key):
        async with pace:
            await sleep(gap)
        try:
            return await ib.reqHistoricalDataAsync(
                contract, end, "60 S", "1 min", side, useRTH=regular, formatDate=2, timeout=timeout
            )
        except Exception as exc:  # a timeout or a dropped request (Mac asleep): recorded, never fatal
            failures[key] = f"{side}: {type(exc).__name__}"
            return []

    async def one(day, code):
        contract = contracts.get(code)
        if contract is None:
            failures[code] = "not qualified"
            return
        key, end = f"{day} {code}", f"{day.replace('-', '')} {close} US/Eastern"
        async with slots:
            sides = {}
            for side in ("BID", "ASK"):
                bars = await ask(contract, end, side, key)
                if not bars:
                    break
                sides[side] = float(bars[-1].close)
        if len(sides) == 2 and 0 <= sides["BID"] <= sides["ASK"]:
            write({"day": day, "code": code, "bid": sides["BID"], "ask": sides["ASK"]})
        else:
            failures.setdefault(key, f"no bars ({', '.join(sides) or 'none'})")

    await asyncio.gather(*(one(day, code) for day, code in todo))
    return failures


def statement_conids(folder):
    """{code: IBKR conId} of every option on a saved statement: an expired contract IBKR no longer
    finds by its symbol may still answer by its conId."""
    found = {}
    for path in sorted(Path(folder).glob("*.json")) if folder else []:
        for p in json.loads(path.read_text()).get("positions", []):
            if p.get("asset") == "OPT" and p.get("conid"):
                code = code_of(
                    p["symbol"].split()[0], p["expiry"].replace("-", ""), p["put_call"], p["strike"]
                )
                found[code] = int(p["conid"])
    return found


async def qualify(ib, codes, conids=None):
    """{code: qualified ib_async contract} for option and share codes IBKR knows. An option is looked
    up by its symbol (this finds one expired the day before, Oct 24), then with includeExpired, then
    by its statement conId."""
    from ib_async import Contract, Option, Stock

    def build(code, attempt):
        parsed = parse(code)
        if not parsed:
            return Stock(code[3:], "SMART", "USD")
        if attempt == 2:
            return Contract(conId=conids[code], exchange="SMART")
        symbol, expiry, right, strike = parsed
        contract = Option(symbol, expiry, strike, right, "SMART", currency="USD")
        contract.includeExpired = attempt == 1
        return contract

    found, left = {}, sorted(codes)
    for attempt in range(3):
        pending = {code: build(code, attempt) for code in left if attempt < 2 or code in (conids or {})}
        items = list(pending.values())
        for start in range(0, len(items), 50):
            await ib.qualifyContractsAsync(*items[start : start + 50])
        found.update({code: c for code, c in pending.items() if c.conId})
        left = [code for code in left if code not in found]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="IBKR 16:00 option books for past days; read-only")
    parser.add_argument("--config", default="config/local.contest.yaml")
    parser.add_argument("--chains", nargs="*", default=[])
    parser.add_argument("--chain-days", nargs="*", type=date.fromisoformat, default=[])
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument("--ladder", type=int, default=4, help="strikes each side of a statement leg")
    parser.add_argument("--moneyness", type=float, default=0.3)
    parser.add_argument("--symbols", nargs="*", help="chain underlyings to keep (default: all)")
    parser.add_argument(
        "--wanted", help="JSON {day: [codes]} to fetch too (ops/rank_history.py --wanted-output)"
    )
    parser.add_argument("--close", default="16:00:00", help="ET end of the one-minute bars")
    parser.add_argument(
        "--client-offset", type=int, default=CLIENT_OFFSET, help="added to the app's client id"
    )
    parser.add_argument("--rate", type=float, default=4.0, help="historical requests a second")
    parser.add_argument("--concurrency", type=int, default=8, help="contracts asked at once (IBKR: 50)")
    parser.add_argument("--timeout", type=float, default=10, help="seconds per request")
    parser.add_argument("--limit", type=int, help="fetch only this many (a quick check first)")
    parser.add_argument("--output", default=str(ROOT / "data/calibration/ibkr-books.json"))
    args = parser.parse_args(argv)
    import yaml
    from ib_async import IB
    from ib_async.ib import StartupFetchNONE

    sys.path.insert(0, str(ROOT))
    from hedge.broker import without_summary_resubscribe

    config = yaml.safe_load(Path(args.config).read_text())
    legs = statement_wants(args.statements, args.ladder, strike_step, listed_strikes(args.chains))
    chains = chain_wants(args.chains, args.chain_days, args.moneyness, args.symbols)
    for day, codes in (json.loads(Path(args.wanted).read_text()) if args.wanted else {}).items():
        chains[day] = list(dict.fromkeys([*chains.get(day, []), *codes]))
    output = Path(args.output)
    rows = json.loads(output.read_text()) if output.exists() else []
    done = {(r["day"], r["code"]) for r in rows}
    todo = plan(legs, chains, done, f"{date.today():%Y%m%d}")[: args.limit]
    days = len(set(legs) | set(chains))
    print(f"{len(done)} books on file; {len(todo)} to fetch over {days} days", flush=True)

    def write(row):
        rows.append(row)
        if len(rows) % 50 == 0:
            save()

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows) + "\n")
        temporary.replace(output)

    async def run():
        ib = without_summary_resubscribe(IB())  # keeps IBKR's two summary slots for the app
        await ib.connectAsync(
            config.get("gateway_host", "127.0.0.1"),
            int(config["gateway_port"]),
            clientId=int(config.get("client_id", 1)) + args.client_offset,
            readonly=True,
            timeout=15,
            fetchFields=StartupFetchNONE,  # historical data only: no positions, orders or account
        )
        try:
            contracts = await qualify(ib, {code for _, code in todo}, statement_conids(args.statements))
            print(f"{len(contracts)} of {len({c for _, c in todo})} contracts qualified", flush=True)
            return await fetch(
                ib,
                contracts,
                todo,
                args.rate,
                write,
                concurrency=args.concurrency,
                timeout=args.timeout,
                close=args.close,
            )
        finally:
            ib.disconnect()

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        failures = asyncio.run(run())
    finally:
        save()
    print(f"{len(rows)} books in {output}; {len(failures)} without one")
    for key, reason in sorted(failures.items())[:20]:
        print("  ", key, reason)


if __name__ == "__main__":
    main()

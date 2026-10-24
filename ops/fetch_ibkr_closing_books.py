"""Each option's 16:00 bid and ask on past days from IBKR's historical data, to tune the option-mark
model (hedge/occ_marks.py) on every October day. Read-only: its own API connection (the app's
client id + 300), historical requests only; never trades.

    .venv/bin/python ops/fetch_ibkr_closing_books.py --config config/local.contest.yaml \\
        --chains data/calibration/chain-prevclose-20261023-2209.json --chain-days 2026-10-22 \\
        [--statements data/calibration/statements] [--moneyness 0.3]

Contracts: every code of each chain file (ops/fetch_moomoo_chain.py) for each --chain-days day, and
every option on each saved statement for its own day with its chain's strikes nearby (--ladder
strikes each side), so a past day's held legs come with enough of their chain to rebuild them.
For each contract and day it asks for the 15:59 one-minute BID and ASK bars (their closes: the
quotes standing at 16:00, the NBBO the clearing house prices from) and, per underlying, the same
for the shares. One-minute bars are not IBKR's "small bars", so the 60-per-10-minutes limit does
not apply; requests still go at most --rate a second. Output: a JSON list of {"day", "code", "bid",
"ask"} (the format ops/mark_backtest.py --chains reads), written as it goes, so a re-run resumes.
"""

import argparse
import asyncio
import json
import re
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


def statement_wants(folder, ladder, step_of):
    """{day: {codes}}: each statement's options and `ladder` strikes each side of each, calls and puts."""
    wanted = {}
    for path in sorted(Path(folder).glob("*.json")) if folder else []:
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") != "OPT":
                continue
            symbol, expiry = p["symbol"].split()[0], p["expiry"].replace("-", "")
            strike, step = float(p["strike"]), step_of(p["symbol"].split()[0], float(p["strike"]))
            for k in range(-ladder, ladder + 1):
                for right in "CP":
                    wanted.setdefault(data["date"], set()).add(
                        code_of(symbol, expiry, right, strike + k * step)
                    )
    return wanted


def strike_step(symbol, strike):
    """Listed strike spacing near `strike` (1 for SPY, 2.5 for most mid-priced shares, 5 above 200)."""
    if symbol in ("SPY", "QQQ", "IWM", "DIA"):
        return 1.0
    return 1.0 if strike < 25 else 2.5 if strike < 200 else 5.0


def chain_wants(paths, days, moneyness):
    """{day: {codes}} of every chain file's codes within `moneyness` of its spot, for every day."""
    codes = set()
    for path in paths:
        chain = json.loads(Path(path).read_text())
        spots = {s: (u or {}).get("spot") for s, u in (chain.get("underlyings") or {}).items()}
        for o in chain.get("options", []):
            spot = spots.get(o["underlying"])
            if spot and abs(o["strike"] / spot - 1) <= moneyness:
                codes.add(o["code"])
    return {str(day): set(codes) for day in days}


def plan(wanted, done):
    """[(day, code)] still to fetch: options expiring on or after the day, and each underlying."""
    todo = []
    for day, codes in sorted(wanted.items()):
        symbols = set()
        for code in sorted(codes):
            parsed = parse(code)
            if parsed and parsed[1] >= day.replace("-", "") and (day, code) not in done:
                todo.append((day, code))
                symbols.add(parsed[0])
        todo += [(day, f"US.{s}") for s in sorted(symbols) if (day, f"US.{s}") not in done]
    return todo


async def fetch(ib, contracts, todo, rate, write, sleep=asyncio.sleep):
    """Ask for each (day, code)'s 15:59 BID and ASK bars, at most `rate` requests a second;
    write({"day", "code", "bid", "ask"}) per success. Returns {code: reason} of failures."""
    failures, gap = {}, 1.0 / rate
    for day, code in todo:
        contract = contracts.get(code)
        if contract is None:
            failures[code] = "not qualified"
            continue
        end = f"{day.replace('-', '')} 16:00:00 US/Eastern"
        sides = {}
        for side in ("BID", "ASK"):
            await sleep(gap)
            try:
                bars = await ib.reqHistoricalDataAsync(
                    contract, end, "60 S", "1 min", side, useRTH=True, formatDate=2, timeout=30
                )
            except Exception as exc:  # a timeout or a dropped request: recorded, never fatal
                bars, failures[f"{day} {code}"] = [], f"{side}: {type(exc).__name__}"
            if bars:
                sides[side] = float(bars[-1].close)
        if len(sides) == 2 and 0 <= sides["BID"] <= sides["ASK"]:
            write({"day": day, "code": code, "bid": sides["BID"], "ask": sides["ASK"]})
        else:
            failures.setdefault(f"{day} {code}", f"no bars ({', '.join(sides) or 'none'})")
    return failures


async def qualify(ib, codes, include_expired=True):
    """{code: qualified ib_async contract} for option and share codes IBKR knows."""
    from ib_async import Option, Stock

    contracts = {}
    for code in sorted(codes):
        parsed = parse(code)
        if parsed:
            symbol, expiry, right, strike = parsed
            contract = Option(symbol, expiry, strike, right, "SMART", currency="USD")
            contract.includeExpired = include_expired
        else:
            contract = Stock(code[3:], "SMART", "USD")
        contracts[code] = contract
    items = list(contracts.items())
    for start in range(0, len(items), 50):
        batch = [c for _, c in items[start : start + 50]]
        await ib.qualifyContractsAsync(*batch)
    return {code: c for code, c in contracts.items() if c.conId}


def main(argv=None):
    parser = argparse.ArgumentParser(description="IBKR 16:00 option books for past days; read-only")
    parser.add_argument("--config", default="config/local.contest.yaml")
    parser.add_argument("--chains", nargs="*", default=[])
    parser.add_argument("--chain-days", nargs="*", type=date.fromisoformat, default=[])
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument("--ladder", type=int, default=4, help="strikes each side of a statement leg")
    parser.add_argument("--moneyness", type=float, default=0.3)
    parser.add_argument("--rate", type=float, default=4.0, help="historical requests a second")
    parser.add_argument("--limit", type=int, help="fetch only this many (a quick check first)")
    parser.add_argument("--output", default=str(ROOT / "data/calibration/ibkr-books.json"))
    args = parser.parse_args(argv)
    import yaml
    from ib_async import IB

    config = yaml.safe_load(Path(args.config).read_text())
    wanted = chain_wants(args.chains, args.chain_days, args.moneyness)
    for day, codes in statement_wants(args.statements, args.ladder, strike_step).items():
        wanted.setdefault(day, set()).update(codes)
    output = Path(args.output)
    rows = json.loads(output.read_text()) if output.exists() else []
    done = {(r["day"], r["code"]) for r in rows}
    todo = plan(wanted, done)[: args.limit]
    print(f"{len(done)} books on file; {len(todo)} to fetch over {len(wanted)} days", flush=True)

    def write(row):
        rows.append(row)
        if len(rows) % 50 == 0:
            save()

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows) + "\n")
        temporary.replace(output)

    async def run():
        ib = IB()
        await ib.connectAsync(
            config.get("gateway_host", "127.0.0.1"),
            int(config["gateway_port"]),
            clientId=int(config.get("client_id", 1)) + CLIENT_OFFSET,
            readonly=True,
            timeout=15,
        )
        try:
            contracts = await qualify(ib, {code for _, code in todo})
            print(f"{len(contracts)} of {len({c for _, c in todo})} contracts qualified", flush=True)
            return await fetch(ib, contracts, todo, args.rate, write)
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

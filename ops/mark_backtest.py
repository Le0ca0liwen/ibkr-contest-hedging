"""Score option-mark estimators against IBKR statement prices, and tune the OCC rebuild.

    .venv/bin/python ops/mark_backtest.py --statements data/calibration/statements \
        --dataset data/calibration/marks-dataset.json [--chains extra.json ...] [--workers 8]

Truth: each statement's Open Positions closing price per option (ops/statement_history.py, or the
flex_statement / statement_positions records in the dataset), and the clearing house's price of
every other listed code: Moomoo's prev_close_price, which equals the statement's Close Price
(Oct 22, all ten legs). Each option_chain record carries the previous session's prices, and
--prev-close FILE=DAY adds a chain file (ops/fetch_moomoo_chain.py) taken before Moomoo rolled DAY.
Inputs per day: every closing book the app recorded at the option's close (option_chain records,
the Moomoo archive's books, the close-mark captures; SPY-style options at 16:00 like the statement,
or 16:15 with --late-at 16:15), the shares' 16:00 close, and optional extra chain files (JSON lists of
{"day", "code", "bid", "ask"}). Estimators: the closing midpoint, hedge/marks.py's local smoothing,
and hedge/occ_marks.py over a parameter grid. Parameters are chosen leave-one-day-out on --select,
so a choice is always judged on a day it was not tuned on. CPU-bound: --workers spreads the grid
over processes (any machine with the repository and its virtualenv, e.g. a PC, can run it).
"""

import argparse
import itertools
import json
import math
import re
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hedge.config import LATE_OPTION_SYMBOLS, NY  # noqa: E402
from hedge.marks import smooth_marks  # noqa: E402
from hedge.occ_marks import occ_marks  # noqa: E402

CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")
GRID = {
    "weight": ["inv_vega2", "inv_vega", "unit", "inv_spread2"],
    "bound_penalty": [1e2, 1e4],
    "rate": [0.034, 0.039],
}


def code_of(symbol, expiry, right, strike):
    return f"US.{symbol}{expiry:%y%m%d}{right}{round(float(strike) * 1000)}"


def parse_code(code):
    match = CODE.match(code)
    if not match:
        return None
    symbol, ymd, right, strike = match.groups()
    return symbol, datetime.strptime(ymd, "%y%m%d").date(), right, int(strike) / 1000


def _date(value):
    if isinstance(value, date):
        return value
    text = str(value or "")[:10].replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else None


def previous_weekday(day):
    day = _date(day) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return str(day)


def load_truth(statements, dataset, prev_close=()):
    """({day: {code: {"mark", "quantity"}}}, {conid: code}): every option on a statement (with its
    position), then every other code's Moomoo prev_close (quantity 0) from the dataset's chains
    (the previous weekday's price) and from `prev_close` [(chain file, day)]."""
    truth, conids = defaultdict(dict), {}

    def add(day, row):
        expiry, strike = _date(row.get("expiry")), row.get("strike")
        right = (row.get("put_call") or row.get("right") or "")[:1].upper()
        symbol = (row.get("underlying") or row.get("symbol") or "").split()[0]
        if not (expiry and strike and right in "CP" and symbol) or row.get("mark") in (None, ""):
            return
        code = code_of(symbol, expiry, right, strike)
        truth[day][code] = {"mark": float(row["mark"]), "quantity": float(row.get("quantity") or 0)}
        if row.get("conid"):
            conids[str(row["conid"])] = code

    label = re.compile(r"^([A-Z.]+) (\d{4}-\d{2}-\d{2}) ([\d.]+)([CP])$")  # the app's Contract.label
    for day, entry in (dataset or {}).get("days", {}).items():
        for record in [*entry.get("flex_statement", []), *entry.get("statement_positions", [])]:
            for conid, row in (record.get("positions") or record.get("rows") or {}).items():
                match = label.match(str(row.get("label", "")))
                if match:
                    symbol, expiry, strike, right = match.groups()
                    price = row.get("mark", row.get("close"))
                    add(
                        day,
                        {
                            "symbol": symbol,
                            "expiry": expiry,
                            "strike": float(strike),
                            "right": right,
                            "mark": price,
                            "quantity": row.get("quantity"),
                            "conid": conid,
                        },
                    )
    for path in sorted(Path(statements).glob("*.json")) if statements else []:
        data = json.loads(path.read_text())
        for p in data.get("positions", []):
            if p.get("asset") == "OPT":
                add(data["date"], p)
            elif p.get("asset") == "STK":
                conids[str(p["conid"])] = f"US.{p['symbol']}"
    chains = [
        (previous_weekday(day), chain)
        for day, entry in (dataset or {}).get("days", {}).items()
        for chain in entry.get("option_chain", [])
    ]
    chains += [(str(_date(day)), json.loads(Path(path).read_text())) for path, day in prev_close]
    for day, chain in chains:
        for o in chain.get("options", []):
            price = o.get("prev_close")
            if price and o.get("code") and o["code"] not in truth[day]:
                truth[day][o["code"]] = {"mark": float(price), "quantity": 0.0}
    return truth, conids


def _late(code):
    """A SPY-style option (or its underlying), whose options close at 16:15."""
    parsed = parse_code(code)
    return (parsed[0] if parsed else code[3:]) in LATE_OPTION_SYMBOLS


def load_books(dataset, conids, extra, late_at="16:00"):
    """({day: {code: (bid, ask)}}, {day: {symbol: spot}}): each option's book at its close, 16:00,
    or late_at for SPY-style options, and each underlying's price at that time."""
    books, spots = defaultdict(dict), defaultdict(dict)
    phase_at = {"pre_close": "16:00", "options_close": "16:15"}

    def wanted(code, at):  # is `at` this code's close?
        return at == (late_at if _late(code) else "16:00")

    for day, entry in (dataset or {}).get("days", {}).items():
        found, underlying = {}, {}
        for at, archived in (entry.get("archive") or {}).items():
            for code, b in archived.items():
                if wanted(code, at):
                    found[code] = (b["bid"], b["ask"])
        for capture in entry.get("close_marks", []):
            at = phase_at.get(capture.get("phase"))
            for conid, q in (capture.get("quotes") or {}).items():
                code = conids.get(str(conid))
                if code and at and wanted(code, at) and q.get("bid") and q.get("ask"):
                    if q.get("data_type") in ("live", "synthetic"):
                        found[code] = (float(q["bid"]), float(q["ask"]))
        for chain in entry.get("option_chain", []):
            at = phase_at.get(chain.get("phase"))
            for o in chain.get("options", []) if at else []:
                if wanted(o["code"], at) and o.get("bid") is not None and o.get("ask") is not None:
                    found[o["code"]] = (o["bid"], o["ask"])
            for symbol, u in (chain.get("underlyings") or {}).items() if at else []:
                if u.get("spot") and wanted(f"US.{symbol}", at):
                    underlying[symbol] = float(u["spot"])
        for record in entry.get("official_closes", []):  # the 16:00 close
            for conid, close in (record.get("closes") or {}).items():
                code = conids.get(str(conid))
                if code and wanted(code, "16:00"):
                    underlying[code[3:]] = float(close)
        for code, (bid, ask) in found.items():
            if not parse_code(code) and bid and ask:
                underlying.setdefault(code[3:], (bid + ask) / 2)
        books[day].update(found)
        spots[day].update(underlying)
    for path in extra or []:
        for row in json.loads(Path(path).read_text()):
            books[row["day"]][row["code"]] = (row["bid"], row["ask"])
    return books, spots


def estimate(day, symbol, expiries, books, spot, params):
    """{code: {estimator: mark}} for one day and underlying."""
    at = datetime.combine(_date(day), time(16), NY)
    chain = []
    for code, (bid, ask) in books.items():
        parsed = parse_code(code)
        if parsed and parsed[0] == symbol and parsed[1] in expiries:
            chain.append(
                {
                    "key": code,
                    "expiry": parsed[1],
                    "strike": parsed[3],
                    "right": parsed[2],
                    "bid": bid,
                    "ask": ask,
                }
            )
    result = defaultdict(dict)
    for row in chain:
        result[row["key"]]["mid"] = (row["bid"] + row["ask"]) / 2
        result[row["key"]]["spread"] = row["ask"] - row["bid"]
    for code, m in smooth_marks(chain, spot, at, params["rate"]).items():
        result[code]["local"] = m["mark"]
    occ = {k: v for k, v in params.items() if k != "rate"}
    for code, m in occ_marks(chain, spot, at, params["rate"], occ).items():
        result[code]["occ"] = m["mark"]
    return dict(result)


def _task(args):
    day, symbol, expiries, books, spot, params = args
    try:
        return day, symbol, params, estimate(day, symbol, expiries, books, spot, params)
    except Exception as exc:  # one bad day must not stop the grid
        return day, symbol, params, {"error": str(exc)}


def run(truth, books, spots, grid=GRID, workers=1):
    combos = [dict(zip(grid, values, strict=True)) for values in itertools.product(*grid.values())]
    tasks = []
    for day, contracts in truth.items():
        groups = defaultdict(set)
        for code in contracts:
            parsed = parse_code(code)
            if parsed and code in books.get(day, {}):
                groups[parsed[0]].add(parsed[1])
        for symbol, expiries in groups.items():
            spot = spots.get(day, {}).get(symbol)
            if spot:
                tasks += [(day, symbol, expiries, books[day], spot, p) for p in combos]
    pool = ProcessPoolExecutor(workers) if workers > 1 else None
    results = list(pool.map(_task, tasks, chunksize=4) if pool else map(_task, tasks))
    if pool:
        pool.shutdown()
    rows = []
    for day, symbol, params, marks in results:
        for code, info in truth[day].items():
            if code not in marks or parse_code(code)[0] != symbol:
                continue
            m = marks[code]
            for name in ("mid", "local", "occ"):
                if name in m:
                    rows.append(
                        {
                            "day": day,
                            "code": code,
                            "params": json.dumps(params, sort_keys=True),
                            "estimator": name,
                            "estimate": m[name],
                            "truth": info["mark"],
                            "quantity": info["quantity"],
                            "spread": m["spread"],
                        }
                    )
    return rows


def summarize(rows):
    """Per (estimator, params): mean |price error|, mean |error| in spreads, and the mean |error of
    the held options' value| per day with positions (None without any)."""
    groups = defaultdict(list)
    for r in rows:
        key = (r["estimator"], r["params"] if r["estimator"] == "occ" else "")
        groups[key].append(r)
    table = {}
    for key, items in groups.items():
        unique = list({(r["day"], r["code"]): r for r in items}.values())
        days = defaultdict(float)
        for r in unique:
            if r["quantity"]:
                days[r["day"]] += r["quantity"] * 100 * (r["estimate"] - r["truth"])
        table[key] = {
            "contracts": len(unique),
            "price_mae": sum(abs(r["estimate"] - r["truth"]) for r in unique) / len(unique),
            "spread_mae": sum(abs(r["estimate"] - r["truth"]) / max(r["spread"], 0.01) for r in unique)
            / len(unique),
            "day_value_mae": sum(abs(v) for v in days.values()) / len(days) if days else None,
            "days": dict(days),
        }
    return table


def _metric(summary, select):
    value = summary.get(select)
    return math.inf if value is None else value


def leave_one_day_out(rows, select="price_mae"):
    """Per day: the OCC parameters chosen on the other days (by `select`) scored on that day, next
    to the midpoint and the local smoothing on the same day."""
    days = sorted({r["day"] for r in rows})
    held = {}
    for day in days:
        occ = [r for r in rows if r["estimator"] == "occ"]
        train = summarize([r for r in occ if r["day"] != day])
        result = {}
        if train:
            best = min(train, key=lambda k: _metric(train[k], select))
            test = summarize([r for r in occ if r["day"] == day and r["params"] == best[1]])
            result["occ"] = {"params": best[1], **next(iter(test.values()), {})}
        for name in ("mid", "local"):
            test = summarize([r for r in rows if r["day"] == day and r["estimator"] == name])
            if test:
                result[name] = next(iter(test.values()))
        held[day] = result
    return held


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--statements", default=str(ROOT / "data/calibration/statements"))
    parser.add_argument("--dataset", default=str(ROOT / "data/calibration/marks-dataset.json"))
    parser.add_argument("--chains", nargs="*", default=[])
    parser.add_argument(
        "--prev-close",
        nargs="*",
        default=[],
        metavar="FILE=DAY",
        help="a chain file taken before Moomoo rolled DAY: its prev_close prices are DAY's truth",
    )
    parser.add_argument("--late-at", choices=["16:00", "16:15"], default="16:00")
    parser.add_argument("--select", choices=["price_mae", "spread_mae", "day_value_mae"], default="price_mae")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", default=str(ROOT / "data/calibration/backtest.json"))
    args = parser.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text()) if Path(args.dataset).exists() else {}
    prev_close = [tuple(item.rsplit("=", 1)) for item in args.prev_close]
    truth, conids = load_truth(args.statements, dataset, prev_close)
    books, spots = load_books(dataset, conids, args.chains, args.late_at)
    rows = run(truth, books, spots, workers=args.workers)
    table = summarize(rows)
    held = leave_one_day_out(rows, args.select)
    held_positions = sum(1 for v in truth.values() for t in v.values() if t["quantity"])
    print(
        f"{sum(len(v) for v in truth.values())} true option prices ({held_positions} held) on "
        f"{len(truth)} days; {len(rows)} scored rows; SPY-style options at {args.late_at}"
    )
    for (name, params), s in sorted(table.items(), key=lambda kv: _metric(kv[1], args.select))[:15]:
        value = s["day_value_mae"]
        print(
            f"{name:6} {s['contracts']:5} contracts  price MAE {s['price_mae']:.4f}  "
            f"spread MAE {s['spread_mae']:.2f}  day value MAE "
            f"{'—' if value is None else f'${value:,.0f}'}  {params}"
        )
    for day, result in held.items():
        parts = [
            f"{name} {r['price_mae']:.4f}"
            + (f" (${r['day_value_mae']:,.0f})" if r.get("day_value_mae") else "")
            for name, r in result.items()
            if r.get("contracts")
        ]
        print(f"held out {day}: price MAE " + ", ".join(parts))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {"rows": rows, "summary": {f"{k[0]} {k[1]}": v for k, v in table.items()}, "held_out": held},
            indent=1,
        )
    )


if __name__ == "__main__":
    main()

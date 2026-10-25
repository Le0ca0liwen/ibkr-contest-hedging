"""Read-only probe: can any Moomoo OpenD route give PAST days' OCC option prices (a) or 16:00 bid/ask (b),
for whole chains and expired contracts? On the Mac, from the repo root, with OpenD logged in:

    .runtime/moomoo-env/bin/python ops/probe_moomoo_history.py [--max-new 4] [--skip rank]

Quote context only (127.0.0.1:11111); never a trade context. Steps, each call failing soft:
kline (request_history_kline, all KL_FIELDs, K_DAY + K_1M 15:50-16:16; quota codes first, <= 4 new by default),
expired (snapshot, option quote and basic info of expired codes; listed codes as the control),
chain (get_option_chain over past expiries), rank (get_option_rank(trading_date=D): depth, whole chains).
Price fields are compared with OCC's price that day and the day before (statements; chain-prevclose file
= Oct 22), rank bid/ask with the collector's 16:00/16:10 books (only when the collector was still writing at that
time: it sometimes stopped at 15:30-15:42, see ops/archive_prev_close.py).
Writes data/calibration/probe-moomoo-history.json and prints which fields matched.

Leads it tests (Oct 25 research): get_option_rank(trading_date=D) returns each contract's bid, ask, mid,
last price and change ratio for a past day D, whole chains filtered by underlying (how far back, and
which moment's book, is undocumented); snapshots of expired codes (option_expiry_date_distance may be
negative); every K-line field. Lines marked "FIX:" are an independent reviewer's corrections.
"""

import argparse
import importlib.util
import json
import math
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True  # leaves no __pycache__ in the repo's ops/ or hedge/
ROOT, NY, TOL, RESERVE = Path.cwd(), ZoneInfo("America/New_York"), 5e-5, 40  # RESERVE: quota never spent
TODAY = datetime.now(NY).date()  # FIX: New York's date, not the Mac's local date
START, END = "2026-10-01", str(TODAY - timedelta(days=1))  # K_DAY bars
OUTPUT = "data/calibration/probe-moomoo-history.json"
CHAIN_TRUTH = "data/calibration/chain-prevclose-20261023-2209.json"  # prev_close = OCC's Oct 22 price
CODE = re.compile(r"^US\.([A-Z.]+?)(\d{6})([CP])(\d+)$")
PRICEY = ("price", "close", "open", "high", "low", "mid", "mark", "bid", "ask", "settle", "prev", "value")
NOT_PRICE = (
    "vol",
    "time",
    "strike",
    "size",
    "count",
    "multipl",
    "distance",
    "turnover",
    "interest",
    "spread",
    "rat",
)  # volumes, times, ratios and rates are not prices
LADDER = [
    "2026-10-23",
    "2026-10-24",
    "2026-10-16",
    "2026-10-01",
    "2026-09-15",
    "2026-07-15",
    "2026-01-15",
    "2025-10-15",
]
TALLY, DROPPED, ERRORS, DATE_ONLY, ECHO = {}, {}, [], [False], {}
sdk = ctx = None  # the moomoo module and the quote context (main)


def num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def call(name, *args, **kwargs):
    """ctx.<name>(...) as (ok, records or error text, rest of the reply); never raises."""
    try:
        ret, data, *rest = getattr(ctx, name)(*args, **kwargs)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", []
    if ret != sdk.RET_OK:
        return False, str(data), rest
    try:
        return True, (json.loads(data.to_json(orient="records")) if hasattr(data, "to_json") else data), rest
    except Exception as exc:  # FIX: a reply that does not serialise fails soft too
        return False, f"{type(exc).__name__}: {exc}", rest


def parts(code):
    """(underlying, expiry yyyy-mm-dd) of an option code; ("", "9999-12-31") for anything else."""
    m = CODE.match(code or "")
    return (m[1], f"20{m[2][:2]}-{m[2][2:4]}-{m[2][4:]}") if m else ("", "9999-12-31")


def prev_day(day):
    d = date.fromisoformat(day) - timedelta(days=1)
    return str(d - timedelta(days=max(0, d.weekday() - 4)))


def prev_close_day(now=None):
    """FIX: the day whose OCC price a snapshot's prev_close_price holds now. Moomoo rolls it on trading
    days only, by ~09:25 ET (SPY's ~07:30), so on a weekend or before 09:30 it is two sessions back."""
    now = now or datetime.now(NY)
    session = str(now.date())
    if now.weekday() > 4 or now.hour * 60 + now.minute < 9 * 60 + 30:
        session = prev_day(session)
    return prev_day(session)


def score(source, row, truths):
    """Tally, per price field of row, the values equal to OCC's price under each truth label."""
    for field, value in (row or {}).items():
        v = num(value)
        if v is None or not any(t in field for t in PRICEY) or any(t in field for t in NOT_PRICE):
            continue
        for label, price in truths.items():
            if price is not None:
                count(f"{source}: {field} == OCC {label}", abs(v - price) < TOL)


def count(key, hit):
    cell = TALLY.setdefault(key, [0, 0])
    cell[0], cell[1] = cell[0] + bool(hit), cell[1] + 1


def truths(by_code, code, day=None):
    """OCC's price on day and the trading day before; without a day, on the day a snapshot's prev_close
    should hold now (FIX) and on the code's last two truth days."""
    t = by_code.get(code, {})
    if day:
        return {"same day": t.get(day), "day before": t.get(prev_day(day))}
    days = sorted(t)[-2:]
    return {
        "expected prev-close day": t.get(prev_close_day()),
        "last truth day": t[days[-1]] if days else None,
        "day before that": t[days[0]] if days[1:] else None,
    }


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_truth():
    """{code: {day: OCC price}}: the statements' Close Price, else the chain file's prev_close (Oct 22)."""
    try:  # FIX: a bad statement file is reported, not fatal
        close = load("probe_close_sources", ROOT / "ops/probe_close_sources.py")
        days = close.statement_prices(ROOT / "data/calibration/statements")
    except Exception as exc:
        ERRORS.append(f"truth statements: {type(exc).__name__}: {exc}")
        days = {}
    try:
        for o in json.loads((ROOT / CHAIN_TRUTH).read_text())["options"]:
            if num(o.get("prev_close")):
                days.setdefault("2026-10-22", {}).setdefault(o["code"], num(o["prev_close"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ERRORS.append(f"truth {CHAIN_TRUTH}: {exc}")
    return {c: {d: p[c] for d, p in days.items() if c in p} for c in {c for p in days.values() for c in p}}


def wrap(cls, name):
    """Count reply bytes the SDK's _pb2 cannot decode: an undocumented field (from a newer OpenD?).
    FIX: also keep the reply's own s2c.tradingDate (get_option_rank echoes it even with no rows)."""
    original, entry = getattr(cls, name), DROPPED.setdefault(cls.__name__, [0, 0])  # replies, unknown bytes

    def patched(rsp_pb, *args, **kwargs):
        try:
            (copy := type(rsp_pb)()).CopyFrom(rsp_pb)
            copy.DiscardUnknownFields()
            size = lambda m: len(m.SerializePartialToString())  # noqa: E731  FIX: ByteSize() raises
            entry[0], entry[1] = (
                entry[0] + 1,
                entry[1] + size(rsp_pb) - size(copy),
            )  # if a required field is unset
            s2c = rsp_pb.s2c
            ECHO[cls.__name__] = (
                s2c.tradingDate
                if s2c.DESCRIPTOR.fields_by_name.get("tradingDate") and s2c.HasField("tradingDate")
                else None
            )
        except Exception as exc:  # never breaks the SDK's own call
            entry.append(repr(exc))
        return original(rsp_pb, *args, **kwargs)

    setattr(cls, name, staticmethod(patched))


@contextmanager
def sdk_patch(wide=False):
    """wide: request_history_kline asks for every needKLFieldsFlag bit, not just the 10 the SDK knows.
    DATE_ONLY: get_option_rank sends trading_date as the proto's yyyy-MM-dd, not 'yyyy-MM-dd 00:00:00'
    (the SDK's normalize_date_format adds the time; Qot_GetOptionRank.proto says yyyy-MM-dd)."""
    import moomoo.quote.quote_query as qq

    saved = sdk.KL_FIELD.__dict__["kl_fields_to_flag_val"], qq.normalize_date_format
    if wide:
        sdk.KL_FIELD.kl_fields_to_flag_val = classmethod(lambda cls, fields: (1 << 31) - 1)
    if DATE_ONLY[0]:
        qq.normalize_date_format = lambda text, *a, **k: (sdk.RET_OK, str(text)[:10])
    try:
        yield
    finally:
        sdk.KL_FIELD.kl_fields_to_flag_val, qq.normalize_date_format = saved


def quota():
    ok, q, _ = call("get_history_kl_quota", get_detail=True)
    return {"used": q[0], "remaining": q[1], "codes": [d.get("code") for d in q[2]]} if ok else {"error": q}


def kline(code, ktype, start=START, end=END):
    time.sleep(0.6)  # 60 requests per 30 s
    return call(
        "request_history_kline",
        code,
        start=start,
        end=end,
        ktype=getattr(sdk.KLType, ktype),
        autype=sdk.AuType.NONE,
        fields=[sdk.KL_FIELD.ALL],
        max_count=1000,
    )


def step_kline(args, by_code, out):
    # The docs give options their own history K-line quota (20/60/200/400 by tier, per option chain =
    # one underlying and expiry, 7 days), which get_history_kl_quota's single used/remaining may not
    # show; RESERVE guards only what that call reports, so --max-new is the real bound.
    out["quota before"] = before = quota()
    held, richest = set(before.get("codes", [])), sorted(by_code, key=lambda c: -len(by_code[c]))
    reused = [c for c in richest if c in held]
    siblings = [c for c in richest if parts(c) in {parts(r) for r in reused}]  # quota per chain or per code?
    fresh = [c for c in dict.fromkeys(args.expired + siblings + richest) if c not in held]
    fresh = fresh[: max(0, min(args.max_new, 10))]
    for code in reused + fresh:
        entry = out[code] = {"truth": by_code.get(code), "new quota code": code in fresh}
        if code in fresh and ((q := quota()).get("remaining") or 0) <= RESERVE:
            entry["skipped"] = f"quota {q}: at or under the reserve of {RESERVE}"
            break
        ok, entry["K_DAY"], _ = kline(code, "K_DAY")
        if code in fresh:
            entry["quota used before, after"] = [q.get("used"), quota().get("used")]
            if not ok and re.search(r"quota|额度", str(entry["K_DAY"]), re.I):  # FIX: stop spending
                entry["stopped"] = "quota error: no further new codes"
                break
        for bar in entry["K_DAY"] if ok else []:
            score("K_DAY", bar, truths(by_code, code, bar["time_key"][:10]))
        for day in [d for d in args.minute_days if d <= parts(code)[1]] if ok else []:
            ok1, bars, _ = kline(code, "K_1M", day, day)
            if ok1:
                bars = [b for b in bars if "15:50" <= b["time_key"][11:16] <= "16:16"]
            entry[f"K_1M {day}"] = bars
            closing = [b for b in bars if b["time_key"][11:19] <= "16:00:00"] if ok1 else []
            score("K_1M bar ending 16:00", closing[-1] if closing else None, truths(by_code, code, day))
            score("K_1M last bar to 16:16", bars[-1] if ok1 and bars else None, truths(by_code, code, day))
    for code in [c for c in reused + fresh if isinstance(out.get(c, {}).get("K_DAY"), list)][:2]:
        dropped = DROPPED.get("RequestHistoryKlineQuery", [0, 0])[1]
        with sdk_patch(wide=True):  # a code already fetched: no new quota
            ok, bars, _ = kline(code, "K_DAY")
        # FIX: the SDK's DataFrame keeps only the columns it knows, so only undecoded bytes can show
        # an extra field; report them for this call.
        out[code]["K_DAY, every flag bit"] = {
            "bars": len(bars) if ok else bars,
            "undecoded bytes": DROPPED.get("RequestHistoryKlineQuery", [0, 0])[1] - dropped,
        }
    out["quota after"] = quota()


def step_expired(args, by_code, out):
    from moomoo.quote.quote_query import OptionStrategyLeg

    today = str(TODAY)
    out["expected prev-close day"] = prev_close_day()
    expired = list(dict.fromkeys(args.expired + sorted(c for c in by_code if parts(c)[1] < today)))[:12]
    listed = sorted(c for c in by_code if parts(c)[1] >= today)[:400]
    if expired:  # FIX: an empty code_list makes the SDK ask for every US derivative's static info
        out["get_stock_basicinfo"] = call(
            "get_stock_basicinfo", sdk.Market.US, sdk.SecurityType.DRVT, expired
        )[1]
    ok, rows, _ = call("get_market_snapshot", listed) if listed else (True, [], [])
    snaps = {r["code"]: [r] for r in rows} if ok else {}
    if not ok:  # FIX: one unknown code fails a whole batch; fall back to single codes for the control
        out["listed batch error"] = rows
        for code in listed[:20]:
            time.sleep(0.6)
            snaps[code] = call("get_market_snapshot", [code])[1]
    for code in expired + listed:
        if code in expired:  # one code a call: one unknown code fails a whole batch
            time.sleep(0.6)
            snaps[code] = call("get_market_snapshot", [code])[1]
        group, rows = "expired" if code in expired else "listed", snaps.get(code)
        for row in rows if isinstance(rows, list) else []:
            score(f"get_market_snapshot ({group})", row, truths(by_code, code))
        if code in expired or code in listed[:8]:
            leg = OptionStrategyLeg()
            leg.code, leg.action, leg.quantity = code, sdk.StrategyLegAction.BUY, 1  # prices a leg; no order
            time.sleep(0.3)
            ok, quoted, _ = call("get_option_quote", [leg])
            for row in quoted if ok else []:
                score(f"get_option_quote ({group})", row, truths(by_code, code))
            out[code] = {"truth": by_code.get(code), "snapshot": rows, "option_quote": quoted}


def step_chain(args, by_code, out):
    for sym, start, end in (
        ("BA", "2026-10-16", "2026-10-16"),
        ("SPY", "2026-10-19", "2026-10-23"),
        ("BA", str(TODAY - timedelta(days=12)), str(TODAY + timedelta(days=14))),
    ):
        time.sleep(3.1)  # 10 option chain requests per 30 s
        ok, rows, _ = call("get_option_chain", code=f"US.{sym}", start=start, end=end)
        expiries = sorted({str(r.get("strike_time"))[:10] for r in rows}) if ok else []
        out[f"chain US.{sym} {start}..{end}"] = {"rows": len(rows), "expiries": expiries} if ok else rows


def rank(sym, day, count=200, pages=1):
    from moomoo.quote.quote_option_event_info import OptionRankFilter  # not exported by moomoo/__init__.py

    owner = OptionRankFilter(sdk.OptionRankIndicatorType.OWNER_LIST, security_list=[f"US.{sym}"])
    rows, page, meta = [], None, {"pages": 0, "date_only": DATE_ONLY[0]}
    while meta["pages"] < pages:
        time.sleep(0.2 if page else 0.6)  # 60 per 30 s; only a paged call's first request counts
        ECHO.pop("GetOptionRank", None)
        with sdk_patch():
            ok, data, rest = call(
                "get_option_rank",
                sdk.OptionMarket.US_SECURITY,
                sdk.OptionRankType.OI,
                count=count,
                trading_date=day,
                page=page,
                filter_list=[owner],
            )
        meta["pages"] += 1
        meta["reply trading_date"] = ECHO.get("GetOptionRank")
        if not ok:
            meta["error"] = data
            break
        (page, meta["all_count"]), rows = rest, rows + data
        if not page:
            break
    meta["echoed trading_date"] = sorted({str(r.get("trading_date")) for r in rows})
    meta["echoes day"] = str(meta.get("reply trading_date") or "").startswith(day)
    return rows, meta


def step_rank(args, by_code, out):
    # FIX: try both date forms on every ladder day. OpenD may ignore a date it cannot parse and send the
    # latest ranking with no error, which the first draft's error-only retry would read as "no history".
    hits = {False: 0, True: 0}
    for day in LADDER:  # how far back does trading_date reach?
        found = {}
        for date_only in (False, True):
            DATE_ONLY[0] = date_only
            rows, meta = rank("SPY", day, count=1)
            hits[date_only] += meta["echoes day"]
            found["date-only form" if date_only else "SDK form"] = {
                **meta,
                "first": rows[0]["code"] if rows else None,
            }
        out.setdefault("depth", {})[day] = found
    DATE_ONLY[0] = hits[True] > hits[False]
    out["date form used for chains"] = "yyyy-MM-dd" if DATE_ONLY[0] else "SDK 'yyyy-MM-dd 00:00:00'"
    for day in args.rank_days:
        books = archive_books(day, out.setdefault(f"archive {day}", {}))
        out[f"archive {day}"].update({k: len(v) for k, v in books.items()})
        booked = set().union(*books.values()) if books else set()
        wanted, rows = {c for c, t in by_code.items() if day in t or prev_day(day) in t} | booked, {}
        for sym in args.underlyings:
            mine = {c for c in wanted if parts(c)[0] == sym}
            got, meta = rank(sym, day, pages=args.rank_pages if mine else 1)
            expiries, codes = [parts(r["code"])[1] for r in got], {r["code"] for r in got}
            out[f"{sym} {day}"] = {
                **meta,
                "rows": len(got),
                "expiries": len(set(expiries)),
                "expired since": sum(e < str(TODAY) for e in expiries),
                "truth or archive codes returned": f"{len(mine & codes)} of {len(mine)}",
            }
            rows.update({r["code"]: r for r in got})
        for code, r in rows.items():
            price, change, prev = num(r.get("option_price")), num(r.get("change_ratio")), None
            if price is not None and change is not None:  # percent (the proto) or a decimal (the doc)?
                r["implied_prev_pct"] = price / (1 + change / 100) if change > -100 else None
                r["implied_prev_dec"] = price / (1 + change) if change > -1 else None
                prev = by_code.get(code, {}).get(prev_day(day))
            if prev:  # FIX: rounding-aware. change_ratio may carry 3 decimals (doc example -67.688), so the
                # rebuilt price can miss by more than 5e-5 on dear options; test the ratio itself instead.
                exact = (price / prev - 1) * 100
                count(
                    "get_option_rank: change_ratio (pct, +-0.0006) consistent with OCC day before",
                    abs(change - exact) <= 6e-4,
                )
                count(
                    "get_option_rank: change_ratio (decimal, +-6e-6) consistent with OCC day before",
                    abs(change - exact / 100) <= 6e-6,
                )
            score("get_option_rank", r if code in by_code else None, truths(by_code, code, day))
            for label, found in books.items():
                for side in ("bid", "ask") if code in found else ():
                    v, book = num(r.get(f"{side}_price")), found[code][side]
                    count(f"rank {side}_price == {label}", v is not None and abs(v - book) < 1e-6)
        out[f"rows {day}"] = [
            {**r, **{k: v.get(c) for k, v in books.items()}}
            for c, r in rows.items()
            if c in by_code or any(c in v for v in books.values())
        ]


def received(line, marker=b'"received_at": "'):
    """An archive line's received_at (aware), read near its start like hedge/moomoo_archive.py; else None."""
    start = line.find(marker, 0, 500)
    if start < 0:
        return None
    try:
        return datetime.fromisoformat(
            line[start + len(marker) : line.index(b'"', start + len(marker))].decode()
        )
    except (ValueError, UnicodeDecodeError):
        return None


def archive_books(day, out=None):
    """{"archive 16:00": {code: book}, "archive 16:10": ...}: each archived code's book standing at the
    cutoff (its last valid book within 30 min before), read by hedge/moomoo_archive.py, loaded without
    hedge/config.py (whose imports this env lacks). FIX: a cutoff counts only when the collector wrote a
    line within a minute of it (it sometimes stopped at 15:30-15:42, and a 15:42 book is not the 16:00
    one); the codes come from the archive itself."""
    found = {}
    try:
        if "hedge.moomoo_archive" not in sys.modules:
            package, config = type(sys)("hedge"), type(sys)("hedge.config")
            package.__path__, config.NY = [str(ROOT / "hedge")], NY
            sys.modules.setdefault("hedge", package), sys.modules.setdefault("hedge.config", config)
            load("hedge.moomoo_archive", ROOT / "hedge/moomoo_archive.py")
        archive = sys.modules["hedge.moomoo_archive"]
        path = archive.archive_path(ROOT / "data/moomoo", date.fromisoformat(day))
        since = datetime.fromisoformat(f"{day}T15:50:00").replace(tzinfo=NY)
        stamps = [t for t in map(received, archive._lines(path)) if t and t.tzinfo]
        codes = set()
        for securities in archive._snapshots(path, since):
            codes |= {c for c in securities if CODE.match(c)}
        for hhmm in ("16:00", "16:10"):  # the collector stops archiving at 16:10
            cutoff = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
            if any(abs(t - cutoff) <= timedelta(minutes=1) for t in stamps):
                found[f"archive {hhmm}"] = archive.last_books(path, dict.fromkeys(codes, cutoff))
            elif out is not None:
                last = max(stamps, default=None)
                out[f"archive {hhmm} skipped"] = f"collector not running then (last line {last})"
    except Exception as exc:
        ERRORS.append(f"archive {day}: {type(exc).__name__}: {exc}")
    return found


def report(result, args):
    print("Fields equal to OCC's price (rank bid/ask: to the archived book), matched/compared:")
    for _, key, m, n in sorted((-m / n, key, m, n) for key, (m, n) in TALLY.items() if m):
        print(f"  {m:5}/{n:<5} {key}")
    print(f"  {sum(not m for m, _ in TALLY.values())} other field/label pairs never matched (see 'matches')")
    kl, rk, ex = result.get("kline", {}), result.get("rank", {}), result.get("expired", {})
    brief = {
        "quota used": [kl.get("quota before", {}).get("used"), kl.get("quota after", {}).get("used")],
        **{c: v["quota used before, after"] for c, v in kl.items() if "quota used before, after" in v},
        **result.get("chain", {}),
        **rk.get("depth", {}),
        **{k: v for k, v in rk.items() if k != "depth" and not k.startswith("rows")},
    }
    for code in args.expired:
        snap, bars = ex.get(code, {}).get("snapshot"), kl.get(code, {}).get("K_DAY")
        if isinstance(snap, list):
            snap = [(r.get("prev_close_price"), r.get("sec_status")) for r in snap]
        brief[code] = {
            "K_DAY": len(bars) if isinstance(bars, list) else bars,
            "prev_close, status": snap,
            "expected prev-close day": ex.get("expected prev-close day"),
        }
    for key, value in brief.items():
        print(f"{key}: {json.dumps(value, default=str)[:300]}")
    print(f"replies, bytes the SDK dropped: {DROPPED}\n{len(ERRORS)} errors; wrote {ROOT / OUTPUT}")


def main(argv=None):
    global sdk, ctx
    p = argparse.ArgumentParser(description="Read-only Moomoo history probe; quote context only")
    p.add_argument("--max-new", type=int, default=4, help="new history K-line quota codes (10 at most)")
    p.add_argument("--minute-days", nargs="*", default=["2026-10-22", "2026-10-16"])
    p.add_argument("--expired", nargs="*", default=["US.BA261016C195000", "US.BA261023C195000"])
    p.add_argument("--rank-days", nargs="*", default=["2026-10-22", "2026-10-16", "2026-10-08", "2026-10-07"])
    p.add_argument("--underlyings", nargs="*", default=["SPY", "RKLB", "SPCX", "BA", "QQQ", "IWM"])
    p.add_argument("--rank-pages", type=int, default=120, help="rank pages of 200 per underlying and day")
    p.add_argument("--skip", nargs="*", default=[], choices=["kline", "expired", "chain", "rank"])
    args = p.parse_args(argv)
    if not (ROOT / "ops/probe_close_sources.py").exists():
        sys.exit("Run this from the repo root: ops/probe_close_sources.py not found")
    by_code = load_truth()
    import moomoo
    import moomoo.quote.quote_query as qq

    sdk = moomoo
    for cls, name in (
        (qq.MarketSnapshotQuery, "unpack_rsp"),
        (qq.GetOptionRank, "unpack_rsp"),
        (qq.GetOptionQuoteQuery, "unpack"),
        (qq.RequestHistoryKlineQuery, "unpack_rsp"),
    ):
        wrap(cls, name)
    days = sorted({d for t in by_code.values() for d in t})
    result = {
        "at": datetime.now(NY).isoformat(),
        "args": vars(args),
        "truth": {d: sum(d in t for t in by_code.values()) for d in days},
    }
    print("truth codes by day:", result["truth"] or "NONE: are data/calibration/statements in place?")
    ctx = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)  # quote context only
    try:
        if hasattr(ctx, "set_sync_query_connect_timeout"):  # FIX: a lost OpenD fails a call, not hangs
            ctx.set_sync_query_connect_timeout(60)
        for step in [s for s in ("kline", "expired", "chain", "rank") if s not in args.skip]:
            try:
                globals()[f"step_{step}"](args, by_code, result.setdefault(step, {}))
            except Exception as exc:  # one broken step leaves the others running
                ERRORS.append(f"{step}: {type(exc).__name__}: {exc}")
    finally:
        ctx.close()
        result.update(matches=TALLY, dropped=DROPPED, errors=ERRORS)
        (ROOT / OUTPUT).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / OUTPUT).write_text(json.dumps(result, default=str, indent=1) + "\n")
    report(result, args)


if __name__ == "__main__":
    main()

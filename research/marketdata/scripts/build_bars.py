"""Parse downloaded intraday/daily JSON (untrusted data) into tidy CSVs + a gap/alignment report.

Usage: python -I build_bars.py RAW_DIR OUT_DIR

Sources (all public, no key):
  * WSJ/MarketWatch charting API (api.wsj.net michelangelo timeseries), 1-minute OHLCV, last 10 sessions
    (the API's maximum for intraday steps).  Bars are START-labelled (09:30 = 09:30:00-09:30:59 ET);
    each completed session also carries an extra 16:00 tick = the official (closing-auction) close.
  * Yahoo chart API 1m/7d and 2m|5m/60d if the retry loop got through (it was HTTP 429 at build time).
  * WSJ daily (P1D, 1 year) OHLCV.
  * CBOE delayed intraday 1-minute OHLC for the CURRENT session only (END-labelled), used as a
    cross-check of the WSJ minute bars.
"""

import json
import os
import sys
from datetime import UTC, datetime, time

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SYMS = ["SPCX", "RKLB", "SPY"]
RAW, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
report = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    report.append(s)


def num(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else np.nan
    except (TypeError, ValueError):
        return np.nan


# ---------------------------------------------------------------- WSJ 1-minute
def wsj(path, sym):
    d = json.load(open(path))
    ticks = d["TimeInfo"]["Ticks"]
    px = d["Series"][0]["DataPoints"]
    vol = d["Series"][1]["DataPoints"] if len(d["Series"]) > 1 else [[None]] * len(ticks)
    rows = []
    for t, p, v in zip(ticks, px, vol, strict=True):
        ts = datetime.fromtimestamp(t / 1000, UTC).astimezone(NY)
        if p is None or p[0] is None:
            continue
        rows.append(dict(timestamp_et=ts, symbol=sym, open=num(p[0]), high=num(p[1]), low=num(p[2]),
                         close=num(p[3]), volume=num(v[0]) if v else np.nan))
    return pd.DataFrame(rows)


frames, official = [], []
for s in SYMS:
    f = os.path.join(RAW, f"wsj_{s}_1m_D10.json")
    df = wsj(f, s)
    tod = df.timestamp_et.dt.time
    close_ticks = df[tod == time(16, 0)]
    for _, r in close_ticks.iterrows():
        official.append(dict(date=r.timestamp_et.date().isoformat(), symbol=s, official_close=r.close))
    df = df[(tod >= time(9, 30)) & (tod < time(16, 0))]
    frames.append(df)
bars = pd.concat(frames, ignore_index=True)
bars["date"] = bars.timestamp_et.dt.date.astype(str)
bars = bars.sort_values(["symbol", "timestamp_et"]).reset_index(drop=True)

# expected grid: every regular-session minute 09:30..15:59 of each session seen, up to the last bar
# available today (the current session may still be open when the data was pulled)
sessions = sorted(bars.date.unique())
last_ts = bars.timestamp_et.max()
grid = []
for d in sessions:
    day = pd.Timestamp(d).date()
    g = pd.date_range(datetime.combine(day, time(9, 30), NY), datetime.combine(day, time(15, 59), NY), freq="1min")
    grid.append(g)
grid = grid[0].append(grid[1:]) if len(grid) > 1 else grid[0]
complete = [d for d in sessions if any((o["date"] == d) for o in official)]
partial = [d for d in sessions if d not in complete]
log(f"WSJ 1m sessions: {len(sessions)} ({sessions[0]} .. {sessions[-1]}); complete (with official 16:00 close): "
    f"{len(complete)}; partial: {partial} (last bar {last_ts.strftime('%Y-%m-%d %H:%M %Z')})")
gap_rows = []
for s in SYMS:
    have = set(bars[bars.symbol == s].timestamp_et)
    for d in sessions:
        g = [t for t in grid if t.date().isoformat() == d and (d in complete or t <= last_ts)]
        miss = [t for t in g if t not in have]
        sub = bars[(bars.symbol == s) & (bars.date == d)]
        gap_rows.append(dict(date=d, symbol=s, expected=len(g), present=len(sub), missing=len(miss),
                             missing_list=" ".join(t.strftime("%H:%M") for t in miss[:30]),
                             zero_volume=int((sub.volume.fillna(0) == 0).sum()),
                             flat_bars=int((sub.high == sub.low).sum())))
gaps = pd.DataFrame(gap_rows)
gaps.to_csv(os.path.join(OUT, "bars_1m_gaps.csv"), index=False)
log("missing minutes per symbol:", gaps.groupby("symbol").missing.sum().to_dict(),
    "| zero-volume bars:", gaps.groupby("symbol").zero_volume.sum().to_dict())
for _, r in gaps[gaps.missing > 0].iterrows():
    log(f"  {r.date} {r.symbol}: {r.missing} missing: {r.missing_list}")

out = bars[["timestamp_et", "date", "symbol", "open", "high", "low", "close", "volume"]].copy()
out["timestamp_et"] = out.timestamp_et.map(lambda t: t.isoformat())
out.to_csv(os.path.join(OUT, "bars_1m.csv"), index=False, float_format="%.4f")

# aligned wide close panel on the full expected grid, forward-filled within session, with fill flags
wide = bars.pivot(index="timestamp_et", columns="symbol", values="close")
grid_live = [t for t in grid if t.date().isoformat() in complete or t <= last_ts]
wide = wide.reindex(pd.DatetimeIndex(grid_live))
filled = wide.isna()
wide = wide.groupby(wide.index.date).ffill().groupby(wide.index.date).bfill()
aligned = pd.DataFrame(index=wide.index)
aligned["date"] = [t.date().isoformat() for t in wide.index]
aligned["minute"] = [t.strftime("%H:%M") for t in wide.index]
for s in SYMS:
    aligned[s] = wide[s]
    aligned[f"{s}_filled"] = filled[s].astype(int)
aligned.index.name = "timestamp_et"
aligned = aligned.reset_index()
aligned["timestamp_et"] = aligned.timestamp_et.map(lambda t: t.isoformat())

# per-session summary: open, last-1m close, official close, realized vol (1m, annualized), range
off = pd.DataFrame(official)
summ = []
for (d, s), g in bars.groupby(["date", "symbol"]):
    r = np.diff(np.log(g.close.to_numpy()))
    oc = off[(off.date == d) & (off.symbol == s)].official_close
    summ.append(dict(date=d, symbol=s, n_bars=len(g), first_bar=g.timestamp_et.min().strftime("%H:%M"),
                     last_bar=g.timestamp_et.max().strftime("%H:%M"), open=g.open.iloc[0], high=g.high.max(),
                     low=g.low.min(), last_1m_close=g.close.iloc[-1],
                     official_close=float(oc.iloc[0]) if len(oc) else np.nan,
                     rv_1m_ann=float(np.sqrt(np.sum(r**2) / max(len(r), 1) * 390 * 252)) if len(r) else np.nan,
                     ret_open_to_last=float(g.close.iloc[-1] / g.open.iloc[0] - 1),
                     volume=float(g.volume.sum())))
summ = pd.DataFrame(summ).sort_values(["symbol", "date"])
prev = summ.groupby("symbol").official_close.shift(1)
summ["close_to_close"] = summ.official_close / prev - 1
summ.to_csv(os.path.join(OUT, "sessions_summary.csv"), index=False, float_format="%.6g")
log("\nper-session summary (official close = WSJ 16:00 closing tick):")
log(summ.to_string(index=False, float_format=lambda v: f"{v:.4g}"))

# ---------------------------------------------------------------- WSJ daily (1y)
drows = []
for s in SYMS:
    f = os.path.join(RAW, f"wsj_{s}_1d_P1Y.json")
    if not os.path.exists(f):
        continue
    d = json.load(open(f))
    for t, p, v in zip(d["TimeInfo"]["Ticks"], d["Series"][0]["DataPoints"], d["Series"][1]["DataPoints"], strict=True):
        if p is None or p[0] is None:
            continue
        day = datetime.fromtimestamp(t / 1000, UTC).date()  # daily ticks are 00:00 UTC of the session date
        drows.append(dict(date=day.isoformat(), symbol=s, open=num(p[0]), high=num(p[1]), low=num(p[2]),
                          close=num(p[3]), volume=num(v[0]) if v else np.nan,
                          partial=int(day.isoformat() in partial)))
daily = pd.DataFrame(drows).sort_values(["symbol", "date"])
daily.to_csv(os.path.join(OUT, "daily_1y.csv"), index=False, float_format="%.4f")
log("\nWSJ daily: " + ", ".join(f"{s} {g.date.min()}..{g.date.max()} ({len(g)} d)" for s, g in daily.groupby("symbol"))
    + f"; rows with partial=1 are the still-open session {partial}")
mm = summ.merge(daily, on=["date", "symbol"], suffixes=("", "_d"))
mm = mm[mm.official_close.notna()]
log("daily close vs 1m official close max |diff|:", (mm.official_close - mm.close).abs().groupby(mm.symbol).max().round(4).to_dict())
econ = os.path.join(os.path.abspath(os.environ.get("RESEARCH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                    "marketdata", "econ", "daily.csv")
if os.path.exists(econ):
    e = pd.read_csv(econ)
    m2 = daily.merge(e[e.symbol.isin(SYMS)], on=["date", "symbol"], suffixes=("", "_nasdaq"))
    log("WSJ daily vs scan-study Nasdaq daily.csv: overlap", len(m2), "rows, max |close diff|",
        (m2.close - m2.close_nasdaq).abs().groupby(m2.symbol).max().round(4).to_dict())

# relative paths (replay real minute moves from any starting spot): close / session open and
# close / previous official close (the overnight gap is included in the latter)
prevclose = {}
for sy in SYMS:
    dd = daily[daily.symbol == sy].set_index("date").close
    for d in sessions:
        prior = dd[dd.index < d]
        prevclose[(d, sy)] = float(prior.iloc[-1]) if len(prior) else np.nan
opens = {(r.date, r.symbol): r.open for r in summ.itertuples()}
for sy in SYMS:
    aligned[f"{sy}_rel_open"] = aligned[sy].to_numpy() / np.array([opens[(d, sy)] for d in aligned.date])
    aligned[f"{sy}_rel_prevclose"] = aligned[sy].to_numpy() / np.array([prevclose[(d, sy)] for d in aligned.date])
aligned.to_csv(os.path.join(OUT, "bars_1m_aligned_close.csv"), index=False, float_format="%.6f")

# ---------------------------------------------------------------- CBOE cross-check (current session)
log("\nCBOE delayed 1m (END-labelled) vs WSJ 1m (START-labelled), current session:")
for s in SYMS:
    f = os.path.join(RAW, f"cboe_intraday_{s}.json")
    if not os.path.exists(f):
        continue
    d = json.load(open(f))
    rows = []
    for r in d.get("data", []):
        ts = datetime.fromisoformat(r["datetime"]).replace(tzinfo=NY) - pd.Timedelta(minutes=1)
        rows.append(dict(timestamp_et=ts, c_close=num(r["price"]["close"]), c_vol=num(r["volume"]["stock_volume"])))
    c = pd.DataFrame(rows)
    w = bars[bars.symbol == s][["timestamp_et", "close", "volume"]]
    m = c.merge(w, on="timestamp_et")
    if len(m):
        rel = (m.c_close / m.close - 1).abs()
        log(f"  {s}: {len(m)} common minutes, close |rel diff| median {rel.median():.2e}, p99 {rel.quantile(.99):.2e}, "
            f"max {rel.max():.2e}; volume ratio median {np.nanmedian(m.c_vol / m.volume):.3f}")

# ---------------------------------------------------------------- Yahoo (if it ever got through)
for s in SYMS:
    for iv, rg in [("1m", "7d"), ("2m", "60d"), ("5m", "60d")]:
        f = os.path.join(RAW, f"yf_{s}_{iv}_{rg}.json")
        if not os.path.exists(f):
            continue
        try:
            r = json.load(open(f))["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            y = pd.DataFrame(dict(timestamp_et=[datetime.fromtimestamp(t, UTC).astimezone(NY) for t in r["timestamp"]],
                                  symbol=s, open=q["open"], high=q["high"], low=q["low"], close=q["close"],
                                  volume=q["volume"]))
            y = y[(y.timestamp_et.dt.time >= time(9, 30)) & (y.timestamp_et.dt.time < time(16, 0))].dropna(subset=["close"])
            y["date"] = y.timestamp_et.dt.date.astype(str)
            fn = os.path.join(OUT, f"yahoo_{iv}_{rg}.csv")
            y2 = y[["timestamp_et", "date", "symbol", "open", "high", "low", "close", "volume"]].copy()
            y2["timestamp_et"] = y2.timestamp_et.map(lambda t: t.isoformat())
            y2.to_csv(fn, mode="a", header=not os.path.exists(fn), index=False, float_format="%.4f")
            log(f"Yahoo {s} {iv}/{rg}: {y.date.nunique()} sessions {y.date.min()}..{y.date.max()}, {len(y)} bars")
            if iv == "1m":
                m = y.merge(bars[bars.symbol == s][["timestamp_et", "close"]], on="timestamp_et", suffixes=("", "_wsj"))
                log(f"   vs WSJ: {len(m)} common minutes, median |rel diff| {(m.close / m.close_wsj - 1).abs().median():.2e}")
        except Exception as ex:  # noqa: BLE001
            log(f"Yahoo {s} {iv}/{rg}: unparsed ({ex})")

open(os.path.join(OUT, "bars_report.txt"), "w").write("\n".join(report) + "\n")

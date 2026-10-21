from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import NY
from .continuity import DataUnavailable
from .models import digest
from .valuation import ValuationEngine

# The app can trade again once the next scored session has been open this long.
OVERNIGHT_REACTION = timedelta(minutes=15)
TRADING_DAY = 365 / 252  # calendar days of model variance in one trading day (sigma^2 / 252)


def overnight_variance(close_day: date, next_day: date) -> float:
    """Modeled trading-time clock, in trading days of spot/IV variance, from a close to the reopen.

    An assumption, not an estimate: one weeknight carries 0.30 of a daily variance and every
    extra calendar day (weekend, holiday) adds 0.10, so Friday close -> Monday open is 0.50.
    Option decay is still repriced in calendar time.
    """
    return 0.30 + 0.10 * ((next_day - close_day).days - 1)


def trading_variance(start: datetime, end: datetime) -> float:
    """Spot/IV variance from start to end in trading days, on the same clock as overnight_variance.

    Open-session time counts in proportion to the session length (early closes included); each
    close -> reopen gap inside the interval counts overnight_variance. Intraday variance is
    therefore not understated by calendar time (a 6-hour remainder is ~0.9 days, not 0.25).
    """
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS")
    first, last = start.astimezone(NY).date(), end.astimezone(NY).date()
    sessions = calendar.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))
    total, previous = 0.0, None
    for session in sessions:
        opened = calendar.session_open(session).to_pydatetime()
        closed = calendar.session_close(session).to_pydatetime()
        overlap = (min(end, closed) - max(start, opened)).total_seconds()
        total += max(0.0, overlap) / (closed - opened).total_seconds()
        if previous is not None and start <= previous[1] and end >= opened:
            total += overnight_variance(previous[0], session.date())
        previous = (session.date(), closed)
    return max(total, 1 / 23400)


@dataclass(frozen=True)
class ScenarioSet:
    ids: tuple[int, ...]
    changes: np.ndarray  # dollar value change per share/contract (includes multiplier)
    weights: np.ndarray
    stress: np.ndarray
    next_changes: np.ndarray | None
    calendar_days: float
    source: str
    identifier: str
    betas: dict[str, float] = field(default_factory=dict)
    rhos: dict[int, float] = field(default_factory=dict)
    beta_source: str = "Unavailable: no validated benchmark history"
    built_at: datetime | None = None
    valuation_inputs: dict[int, tuple[float, float | None]] = field(default_factory=dict)
    # Value change from the scored close to next_session's open + OVERNIGHT_REACTION on the same
    # joint shocks; None on the final scored session (no next session) or when not modeled.
    overnight_changes: np.ndarray | None = None
    next_session: date | None = None
    overnight_variance: float = 0.0
    # Symbol -> correlation of its modeled IV shock with its own spot shock (empty: observed IV paths),
    # and the per-scenario IV shocks applied, aligned with the rows of changes/next_changes.
    spot_vol_correlation: dict[str, float] = field(default_factory=dict)
    iv_shocks: dict[str, np.ndarray] = field(default_factory=dict)
    # Spot/IV variance horizon of `changes` in trading days (calendar_days drives decay and carry).
    trading_days: float = 0.0
    # Scenario symbol -> operator-approved event-caution factor applied to the forecast shocks
    # (docs/AI_OPERATOR_FEATURES.md); empty when none. The stress grid never reads it.
    event_caution: dict[str, float] = field(default_factory=dict)

    # Moomoo re-derives spot from the book mid and recalibrates IV on every read (time to expiry
    # shrinks each call), so exact equality fails on unchanged markets. Tolerate only changes
    # worth less than one cent per share: a half-tick mid move, or IV noise x vega under $0.01.
    PRICE_TOLERANCE = 0.01

    def is_current(self, quotes, now, max_age):
        if (
            self.built_at is None
            or not 0 <= (now - self.built_at).total_seconds() <= max_age
            or not set(self.ids).issubset(quotes)
        ):
            return False
        for i in self.ids:
            spot, iv = self.valuation_inputs.get(i, (None, None))
            q = quotes[i]
            if spot is None or abs(q.spot - spot) > self.PRICE_TOLERANCE + 1e-4 * spot:
                return False
            if (iv is None) != (q.iv is None):
                return False
            if iv is not None and abs(q.iv - iv) * abs(q.vega) > self.PRICE_TOLERANCE:
                return False
        return True

    def __post_init__(self):
        if self.changes.shape[1] != len(self.ids) or self.changes.shape[0] != len(self.weights):
            raise ValueError("Invalid scenario dimensions")
        if not np.isfinite(self.changes).all() or not np.isfinite(self.stress).all():
            raise ValueError("Nonfinite scenario values")
        if (self.weights < 0).any() or not np.isclose(self.weights.sum(), 1):
            raise ValueError("Scenario weights must sum to one")
        if (self.overnight_changes is None) != (self.next_session is None):
            raise ValueError("Overnight changes require their next scored session")
        if self.overnight_changes is not None and (
            self.overnight_changes.shape != self.changes.shape
            or not np.isfinite(self.overnight_changes).all()
        ):
            raise ValueError("Invalid overnight scenario values")


class ScenarioEngine:
    def __init__(self, manifest, history: pd.DataFrame | None = None):
        self.manifest = manifest
        self.valuation = ValuationEngine(manifest)
        self.history = history
        try:
            self.daily_history = (
                pd.read_csv(manifest.daily_stock_history) if manifest.daily_stock_history else None
            )
        except OSError:
            self.daily_history = None  # Not fetched yet: start anyway and wait for the file.
        self.daily_mtime = self._daily_mtime() if self.daily_history is not None else None
        self.daily_cache = {}
        self.beta_cache = {}
        # Forecast close date -> {symbol: factor}; the service wires EventRisk.factors_for (approved
        # event caution). Must be total and cheap: it runs inside every build.
        self.event_caution = lambda day: {}

    def _daily_mtime(self):
        try:
            return Path(self.manifest.daily_stock_history).stat().st_mtime_ns
        except (OSError, TypeError):
            return None

    def refresh_daily_history(self):
        """Pick up a re-fetched daily file (run before each open) without a restart."""
        mtime = self._daily_mtime()
        if mtime is None or mtime == self.daily_mtime:
            return
        try:
            history = pd.read_csv(self.manifest.daily_stock_history)
            closes = pd.to_numeric(history["close"])
            dates = pd.to_datetime(history["date"]).dt.date
            if (
                not np.isfinite(closes).all()
                or (closes <= 0).any()
                or pd.DataFrame({"d": dates, "s": history["symbol"]}).duplicated().any()
            ):
                raise ValueError("Invalid daily history")
        except (OSError, ValueError, KeyError):
            # Reject this version and keep the previous (valid) history; staleness still applies.
            self.daily_mtime = mtime
            return
        self.daily_history, self.daily_mtime = history, mtime
        self.daily_cache.clear()
        self.beta_cache.clear()

    def build(self, snapshot, quotes, now, ids=None):
        self.refresh_daily_history()
        if self.manifest.daily_stock_history and self.daily_history is None:
            raise DataUnavailable(
                f"Daily stock history {self.manifest.daily_stock_history} not found; run fetch_moomoo_daily.py"
            )
        ids = tuple(sorted(ids or quotes.keys()))
        end = self.manifest.next_close(now)
        if end is None:
            raise ValueError("No future scoring close")
        # Day 0 establishes a closing baseline; its price movements and trade
        # costs do not score. Initial hedges forecast baseline-close → Day 1.
        forecast_start = (
            self.manifest.session_close(self.manifest.day_zero)
            if now.astimezone(NY).date() == self.manifest.day_zero
            else now
        )
        days = max((end - forecast_start).total_seconds() / 86400, 1 / 86400)
        # Option decay and carry run on calendar days; spot/IV variance on the trading-time clock.
        horizon = trading_variance(forecast_start, end)
        symbols = sorted({self.manifest.contract_map[i].symbol for i in ids})
        rng = np.random.default_rng(self.manifest.policy.seed)
        n = 64
        normals = rng.standard_normal((n // 2, len(symbols) + 1))
        normals = np.vstack([normals, -normals])  # no invented directional drift
        # Drawn after the spot block (spot paths unchanged) and antithetic with it, so each
        # mirrored pair flips both the spot shock and the whole IV shock.
        vol_normals = rng.standard_normal((n // 2, len(symbols)))
        vol_normals = np.vstack([vol_normals, -vol_normals])
        vols = {
            s: next(
                (q.iv for i, q in quotes.items() if self.manifest.contract_map[i].symbol == s and q.iv), 0.25
            )
            for s in symbols
        }
        shocks = None
        spot_vol = {}
        if self.history is not None and not self.history.empty:
            # Input is point-in-time archived quote observations, never future rows.
            # Matched-clock paths carry observed IV changes: keep their empirical spot-vol link.
            historical = self._remaining_paths(now, end, symbols)
            if historical is not None:
                shocks, iv_shocks = historical
                n = len(next(iter(shocks.values())))
                source = "historical matched-clock joint spot/IV paths"
        if shocks is None:
            if self.daily_history is not None:
                covariance = self.daily_covariance(now, symbols)
                joint = normals[:, 1:] @ np.linalg.cholesky(covariance).T
                standardized = joint / np.sqrt(np.diag(covariance))
                shocks = dict(zip(symbols, (joint * np.sqrt(horizon)).T, strict=True))
                source = "prior daily joint returns, shrunk covariance; modeled intraday/IV scaling"
            else:
                standardized = 0.6 * normals[:, :1] + 0.8 * normals[:, 1:]
                shocks = {
                    s: vols[s] * np.sqrt(horizon / 252) * standardized[:, j] for j, s in enumerate(symbols)
                }
                source = "modeled spot/IV scenarios; NOT historical performance evidence"
            # iv_s = sigma_iv * (rho_s * z_spot_s + sqrt(1 - rho_s^2) * u_s), z_spot_s unit variance.
            spot_vol = {s: self.manifest.spot_vol_rho(s) for s in symbols}
            rho = np.array([spot_vol[s] for s in symbols])
            # With a fixed seed the same draws recur all contest; make each u column exactly
            # orthogonal to its own spot z (same norm) so the realized correlation equals rho.
            # Projections are sign-symmetric, so antithetic pairs stay antithetic.
            vol_normals = vol_normals.copy()
            for j in range(len(symbols)):
                z = standardized[:, j]
                u = vol_normals[:, j] - (vol_normals[:, j] @ z) / (z @ z) * z
                if np.linalg.norm(u) > 1e-12:
                    vol_normals[:, j] = u * np.linalg.norm(z) / np.linalg.norm(u)
            iv = (
                0.015
                * np.sqrt(horizon * TRADING_DAY)
                * (rho * standardized + np.sqrt(1 - rho**2) * vol_normals)
            )
            iv_shocks = dict(zip(symbols, iv.T, strict=True))
            source += "; spot-vol corr " + " ".join(f"{s} {r:+.2f}" for s, r in spot_vol.items())
        if (
            len(symbols) > 1
            and self.daily_history is None
            and not source.startswith("historical matched-clock")
        ):
            raise ValueError("Joint historical coverage required for a cross-underlying hedge universe")
        close_day = end.astimezone(NY).date()
        # Operator-approved event caution (docs/AI_OPERATOR_FEATURES.md): scales this session's
        # FORECAST spot and IV shocks only (changes, next_changes, overnight_changes all reuse them).
        # stress_matrix and market_stress never read shocks/iv_shocks, so the 3% cap is unchanged.
        caution = {
            s: float(f)
            for s, f in sorted(self.event_caution(close_day).items())
            if s in shocks and float(f) in (1.25, 1.5)
        }
        for s, f in caution.items():
            shocks = {**shocks, s: shocks[s] * f}
            iv_shocks = {**iv_shocks, s: iv_shocks[s] * f}
        if caution:
            source += "; event caution " + " ".join(f"{s} x{f:.2f}" for s, f in caution.items())
        changes = self._reprice(ids, quotes, forecast_start, end, shocks, iv_shocks)
        stress = self.stress_matrix(ids, quotes, now, max(end, now + timedelta(days=1)))
        betas, beta_source = self.market_betas(now, symbols)
        if all(symbol in betas for symbol in symbols):
            market_stress = [
                [
                    self.manifest.contract_map[i].multiplier
                    * self.valuation.scenario_change(
                        self.manifest.contract_map[i],
                        quotes[i],
                        now,
                        max(end, now + timedelta(days=1)),
                        float(np.clip(betas[self.manifest.contract_map[i].symbol] * move, -0.8, 2)),
                    )
                    for i in ids
                ]
                for move in (-0.05, 0.05)
            ]
            stress = np.vstack([stress, market_stress])
        rhos = {
            i: self.valuation.rho(self.manifest.contract_map[i], quotes[i].spot, quotes[i].iv, now)
            for i in ids
        }
        next_session = self.manifest.next_scored_session(close_day)
        next_changes = overnight_changes = None
        variance = 0.0
        if next_session:
            # Same standardized joint shocks, longer calendar horizon; score is
            # next-close minus this-close, never versus the initial account NAV.
            following = self.manifest.session_close(next_session)
            scale = np.sqrt(trading_variance(end, following) / horizon)
            next_changes = self._reprice(ids, quotes, end, following, shocks, iv_shocks, scale)
            # Close -> reopen: calendar-time decay, trading-time spot/IV variance on the same shocks.
            variance = overnight_variance(close_day, next_session)
            overnight_shocks, overnight_iv = shocks, iv_shocks
            if source.startswith("historical matched-clock"):
                # Realized rest-of-session paths carry a sample mean: no directional overnight drift.
                overnight_shocks = {k: v - v.mean() for k, v in shocks.items()}
                overnight_iv = {k: v - v.mean() for k, v in iv_shocks.items()}
            overnight_changes = self._reprice(
                ids,
                quotes,
                end,
                self.manifest.session_open(next_session) + OVERNIGHT_REACTION,
                overnight_shocks,
                overnight_iv,
                np.sqrt(variance / horizon),
            )
        weights = np.full(n, 1 / n)
        identifier = digest(
            {
                "ids": ids,
                "changes": changes.tolist(),
                "stress": stress.tolist(),
                "end": end,
                "source": source,
                "config": self.manifest.config_hash,
                "betas": betas,
                "rhos": rhos,
                "overnight": overnight_changes.tolist() if overnight_changes is not None else None,
                "spot_vol_correlation": spot_vol,
                **({"event_caution": caution} if caution else {}),  # default builds keep their identifier
            }
        )
        return ScenarioSet(
            ids,
            changes,
            weights,
            stress,
            next_changes,
            days,
            source,
            identifier,
            betas,
            rhos,
            beta_source,
            now,
            {i: (quotes[i].spot, quotes[i].iv) for i in ids},
            overnight_changes,
            next_session,
            variance,
            spot_vol,
            iv_shocks,
            horizon,
            caution,
        )

    def _reprice(self, ids, quotes, start, finish, shocks, iv_shocks, scale=1.0):
        changes = np.empty((len(next(iter(shocks.values()))), len(ids)))
        for j, identifier in enumerate(ids):
            c, q = self.manifest.contract_map[identifier], quotes[identifier]
            current = self.valuation.price(c, q.spot, q.iv, start) if c.kind == "OPT" and q.iv else None
            for s in range(len(changes)):
                changes[s, j] = c.multiplier * self.valuation.scenario_change(
                    c,
                    q,
                    start,
                    finish,
                    float(np.clip(shocks[c.symbol][s] * scale, -0.8, 2)),
                    float(iv_shocks[c.symbol][s] * scale),
                    current=current,
                )
        return changes

    def market_betas(self, now, symbols):
        """OLS beta from aligned completed sessions; missing data is never beta=1."""
        benchmark = self.manifest.beta_benchmark
        key = (now.astimezone(NY).date(), tuple(symbols))
        if key in self.beta_cache:
            return self.beta_cache[key]
        unavailable = ({}, f"Unavailable: need valid aligned history including {benchmark}")
        if self.daily_history is None:
            return unavailable
        frame = self.daily_history.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame = frame[frame.date < key[0]]
        required = sorted(set(symbols) | {benchmark})
        frame = frame[frame.symbol.isin(required)]
        if frame.duplicated(["date", "symbol"]).any():
            return unavailable
        prices = frame.pivot(index="date", columns="symbol", values="close").sort_index()
        if not set(required).issubset(prices.columns):
            return unavailable
        prices = prices[required].dropna().tail(self.manifest.policy.history_sessions + 1)
        if (
            len(prices) < 21
            or (key[0] - prices.index[-1]).days > 7
            or not np.isfinite(prices.to_numpy()).all()
            or (prices <= 0).any().any()
        ):
            return unavailable
        returns = prices.pct_change(fill_method=None).dropna()
        if not np.isfinite(returns.to_numpy()).all():
            return unavailable
        variance = float(returns[benchmark].var())
        if not np.isfinite(variance) or variance <= 1e-12:
            return unavailable
        betas = {s: float(returns[s].cov(returns[benchmark]) / variance) for s in symbols}
        if not all(np.isfinite(value) for value in betas.values()):
            return unavailable
        result = (
            betas,
            f"{benchmark} OLS beta; {len(returns)} completed aligned returns through {prices.index[-1]}",
        )
        self.beta_cache[key] = result
        return result

    def daily_covariance(self, now, symbols):
        """Aligned completed sessions only; no drift or future daily closes."""
        key = (now.astimezone(NY).date(), tuple(symbols), self.manifest.policy.history_sessions)
        if key in self.daily_cache:
            return self.daily_cache[key]
        frame = self.daily_history.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame = frame[frame.date < now.astimezone(NY).date()]
        if frame.duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate daily stock observations")
        prices = frame.pivot(index="date", columns="symbol", values="close").sort_index()
        # Missing or stale history is recoverable by re-running the daily fetch: wait, don't hard-stop.
        if not set(symbols).issubset(prices.columns):
            raise DataUnavailable("Missing joint stock history for hedge universe; re-run the daily fetch")
        prices = prices[list(symbols)].dropna()
        if prices.empty or (now.astimezone(NY).date() - prices.index[-1]).days > 7:
            raise DataUnavailable("Joint stock history is stale; re-run the daily fetch")
        returns = prices.pct_change(fill_method=None).dropna().tail(self.manifest.policy.history_sessions)
        if len(returns) < 20 or not np.isfinite(returns.to_numpy()).all() or (prices <= 0).any().any():
            raise ValueError("At least 20 aligned completed-session returns required")
        empirical = returns.cov().to_numpy()
        covariance = 0.8 * empirical + 0.2 * np.diag(np.diag(empirical))
        covariance += np.eye(len(symbols)) * max(1e-10, np.trace(covariance) * 1e-6)
        self.daily_cache[key] = covariance
        return covariance

    def stress_matrix(self, ids, quotes, now, end):
        symbols = sorted({self.manifest.contract_map[i].symbol for i in ids})
        shifts = [
            (r, v, skew)
            for r in (-0.2, -0.1, -0.05, 0, 0.05, 0.1, 0.2)
            for v in (-0.15, -0.05, 0, 0.05, 0.15)
            for skew in (-0.1, 0.1)
        ]
        # Global correlated shocks plus individual underlying shocks.
        rows = []
        repriced = {}
        for affected in [None, *symbols] if len(symbols) > 1 else [None]:
            for r, v, skew in shifts:
                row = []
                for i in ids:
                    c, q = self.manifest.contract_map[i], quotes[i]
                    applies = affected is None or c.symbol == affected
                    key = (i, r if applies else 0, v if applies else 0, skew if applies else 0)
                    if key not in repriced:
                        repriced[key] = c.multiplier * self.valuation.scenario_change(
                            c, q, now, end, *key[1:]
                        )
                    row.append(repriced[key])
                rows.append(row)
        # Parallel rate shocks supplement spot/IV/skew/time stress, with full
        # American-option repricing rather than a linear rho approximation.
        for rate_shift in (-self.manifest.risk.rate_stress_shift, self.manifest.risk.rate_stress_shift):
            rows.append(
                [
                    self.manifest.contract_map[i].multiplier
                    * self.valuation.scenario_change(
                        self.manifest.contract_map[i], quotes[i], now, end, 0, rate_shift=rate_shift
                    )
                    for i in ids
                ]
            )
        return np.array(rows)

    def _remaining_paths(self, now, end, symbols):
        frame = self.history.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame[frame.timestamp < pd.Timestamp(now)]
        if frame.empty or not {"spot", "iv", "symbol"}.issubset(frame.columns):
            return None
        from .config import NY

        frame["day"] = frame.timestamp.dt.tz_convert(NY).dt.date
        frame["minute"] = (
            frame.timestamp.dt.tz_convert(NY).dt.hour * 60 + frame.timestamp.dt.tz_convert(NY).dt.minute
        )
        clock = now.astimezone(NY).hour * 60 + now.astimezone(NY).minute
        closes = end.astimezone(NY).hour * 60 + end.astimezone(NY).minute
        days = sorted(frame.day.unique())[-self.manifest.policy.history_sessions :]
        returns, ivs = {s: [] for s in symbols}, {s: [] for s in symbols}
        for day in days:
            entries = {}
            for symbol in symbols:
                part = frame[(frame.day == day) & (frame.symbol == symbol)].sort_values("timestamp")
                start, finish = part[part.minute <= clock].tail(1), part[part.minute <= closes].tail(1)
                if (
                    start.empty
                    or finish.empty
                    or abs(int(start.iloc[0].minute) - clock) > 5
                    or abs(int(finish.iloc[0].minute) - closes) > 5
                ):
                    break
                a, b = start.iloc[0], finish.iloc[0]
                if not np.isfinite([a.spot, b.spot, a.iv, b.iv]).all():
                    break
                entries[symbol] = (b.spot / a.spot - 1, b.iv - a.iv)
            if len(entries) == len(symbols):
                for symbol, (ret, iv) in entries.items():
                    returns[symbol].append(ret)
                    ivs[symbol].append(iv)
        if len(returns[symbols[0]]) < 10:
            return None
        return {s: np.array(v) for s, v in returns.items()}, {s: np.array(v) for s, v in ivs.items()}

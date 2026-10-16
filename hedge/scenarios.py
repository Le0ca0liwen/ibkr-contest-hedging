from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .config import NY
from .models import digest
from .valuation import ValuationEngine


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

    def is_current(self, quotes, now, max_age):
        return (
            self.built_at is not None
            and 0 <= (now - self.built_at).total_seconds() <= max_age
            and set(self.ids).issubset(quotes)
            and self.valuation_inputs == {i: (quotes[i].spot, quotes[i].iv) for i in self.ids}
        )

    def __post_init__(self):
        if self.changes.shape[1] != len(self.ids) or self.changes.shape[0] != len(self.weights):
            raise ValueError("Invalid scenario dimensions")
        if not np.isfinite(self.changes).all() or not np.isfinite(self.stress).all():
            raise ValueError("Nonfinite scenario values")
        if (self.weights < 0).any() or not np.isclose(self.weights.sum(), 1):
            raise ValueError("Scenario weights must sum to one")


class ScenarioEngine:
    def __init__(self, manifest, history: pd.DataFrame | None = None):
        self.manifest = manifest
        self.valuation = ValuationEngine(manifest)
        self.history = history
        self.daily_history = (
            pd.read_csv(manifest.daily_stock_history) if manifest.daily_stock_history else None
        )
        self.daily_cache = {}
        self.beta_cache = {}

    def build(self, snapshot, quotes, now, ids=None):
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
        symbols = sorted({self.manifest.contract_map[i].symbol for i in ids})
        rng = np.random.default_rng(self.manifest.policy.seed)
        n = 64
        normals = rng.standard_normal((n // 2, len(symbols) + 1))
        normals = np.vstack([normals, -normals])  # no invented directional drift
        vols = {
            s: next(
                (q.iv for i, q in quotes.items() if self.manifest.contract_map[i].symbol == s and q.iv), 0.25
            )
            for s in symbols
        }
        shocks = {
            s: vols[s] * np.sqrt(days / 365) * (0.6 * normals[:, 0] + 0.8 * normals[:, j + 1])
            for j, s in enumerate(symbols)
        }
        iv_shocks = {s: 0.015 * np.sqrt(days) * normals[:, j + 1] for j, s in enumerate(symbols)}
        source = "modeled spot/IV scenarios; NOT historical performance evidence"
        if self.history is not None and not self.history.empty:
            # Input is point-in-time archived quote observations, never future rows.
            historical = self._remaining_paths(now, end, symbols)
            if historical is not None:
                shocks, iv_shocks = historical
                n = len(next(iter(shocks.values())))
                source = "historical matched-clock joint spot/IV paths"
        if self.daily_history is not None:
            if source.startswith("historical matched-clock"):
                pass  # richer intraday history takes precedence
            else:
                covariance = self.daily_covariance(now, symbols)
                shocks = dict(
                    zip(
                        symbols,
                        (normals[:, 1:] @ np.linalg.cholesky(covariance).T * np.sqrt(days * 252 / 365)).T,
                        strict=True,
                    )
                )
                source = "prior daily joint returns, shrunk covariance; modeled intraday/IV scaling"
        if (
            len(symbols) > 1
            and self.daily_history is None
            and not source.startswith("historical matched-clock")
        ):
            raise ValueError("Joint historical coverage required for a cross-underlying hedge universe")
        changes = np.empty((n, len(ids)))
        for j, identifier in enumerate(ids):
            c, q = self.manifest.contract_map[identifier], quotes[identifier]
            for s in range(n):
                changes[s, j] = c.multiplier * self.valuation.scenario_change(
                    c,
                    q,
                    forecast_start,
                    end,
                    float(np.clip(shocks[c.symbol][s], -0.8, 2)),
                    float(iv_shocks[c.symbol][s]),
                )
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
        following = next(
            (
                self.manifest.session_close(d)
                for d in self.manifest.scoring_sessions
                if self.manifest.session_close(d) > end
            ),
            None,
        )
        next_changes = None
        if following:
            # Same standardized joint shocks, longer calendar horizon; score is
            # next-close minus this-close, never versus the initial account NAV.
            next_days = (following - end).total_seconds() / 86400
            next_changes = np.empty_like(changes)
            for j, identifier in enumerate(ids):
                c, q = self.manifest.contract_map[identifier], quotes[identifier]
                scale = np.sqrt(next_days / days)
                for s in range(n):
                    next_changes[s, j] = c.multiplier * self.valuation.scenario_change(
                        c,
                        q,
                        end,
                        following,
                        float(np.clip(shocks[c.symbol][s] * scale, -0.8, 2)),
                        float(iv_shocks[c.symbol][s] * scale),
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
        )

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
        if not set(symbols).issubset(prices.columns):
            raise ValueError("Missing joint stock history for hedge universe")
        prices = prices[list(symbols)].dropna()
        if prices.empty or (now.astimezone(NY).date() - prices.index[-1]).days > 7:
            raise ValueError("Joint stock history is stale")
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

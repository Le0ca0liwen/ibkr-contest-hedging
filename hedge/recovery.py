"""Deterministic intraday forecasts and bounded recovery, never broker access."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from .config import NY
from .models import Frozen, digest


class RecoveryConfig(Frozen):
    mode: Literal["off", "shadow", "paper"] = "off"
    symbols: tuple[str, ...] = ("SPY", "QQQ")
    model_path: str = "data/recovery/model.json"
    bars_path: str = "data/recovery/intraday.parquet"
    live_bars_path: str = "data/moomoo/live-bars.csv"
    evidence_path: str = "data/recovery/validation.json"
    window_sessions: Literal[20, 60, 120] = 60
    incremental_stress: float = Field(default=0.0025, gt=0, le=0.0025)
    gross_notional: float = Field(default=0.05, gt=0, le=0.05)
    daily_loss_stop: float = Field(default=0.0025, gt=0, le=0.0025)
    target_band: float = Field(default=250, ge=0)
    max_bar_age: int = Field(default=180, ge=60, le=180)
    max_signal_age: int = Field(default=120, ge=15, le=120)
    max_model_age_days: int = Field(default=14, ge=1, le=14)

    @model_validator(mode="after")
    def valid_symbols(self):
        if not 1 <= len(self.symbols) <= 4 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Recovery requires 1–4 unique qualified stock symbols")
        return self


def normalize_bars(frame, cutoff=None):
    """Timestamps denote conservative bar availability, never receipt time."""
    required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame):
        raise ValueError("Recovery bars need interval-end timestamps, symbols, OHLC and volume")
    f = frame.copy()
    f["timestamp"] = pd.to_datetime(f.timestamp, utc=True)
    if cutoff is not None:
        f = f[f.timestamp <= pd.Timestamp(cutoff)]
    if f.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("Duplicate recovery bars")
    if f.empty:
        raise ValueError("Recovery bar archive empty")
    for column in ["open", "high", "low", "close", "volume"]:
        f[column] = pd.to_numeric(f[column], errors="raise")
    if not np.isfinite(f[["open", "high", "low", "close", "volume"]].to_numpy()).all():
        raise ValueError("Nonfinite recovery bars")
    if (f[["open", "high", "low", "close"]] <= 0).any().any() or (f.volume < 0).any():
        raise ValueError("Invalid recovery prices/volume")
    if (
        (f.high < f[["open", "close", "low"]].max(axis=1))
        | (f.low > f[["open", "close", "high"]].min(axis=1))
    ).any():
        raise ValueError("Inconsistent recovery OHLC")
    local = f.timestamp.dt.tz_convert(NY)
    minute = local.dt.hour * 60 + local.dt.minute
    f = f[(minute > 570) & (minute <= 960)]
    f["day"] = f.timestamp.dt.tz_convert(NY).dt.date
    f = f.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    f.attrs["recovery_normalized"] = True
    return f


def features(part, previous_close, kind, close_minute=960):
    """Market information only. NAV deficit is deliberately absent."""
    if len(part) < 61 or previous_close <= 0:
        return None
    differences = part.timestamp.diff().dt.total_seconds().to_numpy()
    if (differences[-60:] != 60).any():
        return None  # a sparse/missing minute is not silently a 60-minute return
    close, volume = part.close.to_numpy(), part.volume.to_numpy()
    last = close[-1]
    vol = float(np.std(np.diff(np.log(close[-61:]))))
    vwap = float(np.average((part.high + part.low + part.close) / 3, weights=np.maximum(volume, 1)))
    gap = float(part.open.iloc[0] / previous_close - 1)
    opening = float(last / part.open.iloc[0] - 1)
    returns = [float(last / close[-n - 1] - 1) for n in (5, 15, 60)]
    pace = float(np.log((volume[-15:].mean() + 1) / (volume[-60:-15].mean() + 1)))
    remaining = (
        close_minute
        - (part.timestamp.iloc[-1].tz_convert(NY).hour * 60 + part.timestamp.iloc[-1].tz_convert(NY).minute)
    ) / 390
    if kind == "momentum":
        return np.array([gap, opening, *returns, vol, pace, remaining])
    return np.array([gap, last / vwap - 1, *returns, vol, pace, remaining])


def training_samples(frame, manifest, symbols, kind, before, window=60):
    f = normalize_bars(frame, before)
    f = f[f.day < before.astimezone(NY).date()]
    rows = []
    for symbol in symbols:
        groups = list(f[f.symbol == symbol].groupby("day", sort=True))
        groups = groups[-(window + 1) :]
        previous = None
        for day, part in groups:
            if not __import__("exchange_calendars").get_calendar("XNYS").is_session(pd.Timestamp(day)):
                continue
            end = manifest.session_close(day)
            part = part[part.timestamp <= pd.Timestamp(end)]
            if part.empty or abs((part.timestamp.iloc[-1].to_pydatetime() - end).total_seconds()) > 1:
                previous = None
                continue
            finish = float(part.close.iloc[-1])
            if previous is not None:
                for index in range(60, len(part) - 15, 5):
                    sample = part.iloc[: index + 1]
                    x = features(
                        sample, previous, kind, end.astimezone(NY).hour * 60 + end.astimezone(NY).minute
                    )
                    if x is not None:
                        rows.append(
                            (
                                symbol,
                                day,
                                sample.timestamp.iloc[-1].isoformat(),
                                x,
                                finish / float(sample.close.iloc[-1]) - 1,
                            )
                        )
            previous = finish
    return rows


def fit_models(frame, manifest, config, before):
    models = []
    for kind in ("momentum", "reversal"):
        rows = training_samples(frame, manifest, config.symbols, kind, before, config.window_sessions)
        for symbol in config.symbols:
            part = [r for r in rows if r[0] == symbol]
            days = sorted({r[1] for r in part})
            if len(days) < 20:
                raise ValueError(f"At least 20 completed training sessions required: {symbol}/{kind}")
            x = np.array([r[3] for r in part])
            y = np.array([r[4] for r in part])
            mean = x.mean(axis=0)
            scale = np.maximum(x.std(axis=0), 1e-6)
            design = np.column_stack([np.ones(len(x)), (x - mean) / scale])
            penalty = np.diag([0.0, *([10.0] * x.shape[1])])
            inverse = np.linalg.inv(design.T @ design + penalty)
            coefficient = inverse @ design.T @ y
            residual = y - design @ coefficient
            # Cluster the parameter uncertainty by session rather than pretending
            # overlapping intraday labels are independent observations.
            meat = np.zeros_like(inverse)
            for day in days:
                indexes = np.array([r[1] == day for r in part])
                score = design[indexes].T @ residual[indexes]
                meat += np.outer(score, score)
            covariance = inverse @ meat @ inverse * len(days) / (len(days) - 1)
            conservative_residual = y - 0.5 * (design @ coefficient)
            clock_bins = np.array(
                [
                    pd.Timestamp(r[2]).tz_convert(NY).hour * 2
                    + pd.Timestamp(r[2]).tz_convert(NY).minute // 30
                    for r in part
                ]
            )
            by_clock = {
                str(int(b)): float(np.std(conservative_residual[clock_bins == b]))
                for b in np.unique(clock_bins)
            }
            models.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "mean": mean.tolist(),
                    "scale": scale.tolist(),
                    "coefficients": coefficient.tolist(),
                    "covariance": covariance.tolist(),
                    "residual_sd": float(residual.std()),
                    "residual_sd_by_clock": by_clock,
                    "sessions": len(days),
                    "samples": len(part),
                    "trained_through": str(days[-1]),
                }
            )
    training_frame = normalize_bars(frame, before)
    training_frame = training_frame[training_frame.day < before.astimezone(NY).date()]
    bundle = {
        "schema": 1,
        "horizon": "scoring_close",
        "method": "ridge10/session-cluster/shrink0.5/clock-residual-v1",
        "symbols": list(config.symbols),
        "data_hash": digest(training_frame.drop(columns="day").to_json(date_format="iso")),
        "trained_before": before.isoformat(),
        "models": models,
    }
    # Hash is recalculated and verified before every load and promotion.
    bundle["model_hash"] = digest(bundle)
    return bundle


def verify_bundle(bundle, config):
    expected = bundle.get("model_hash")
    if expected != digest({k: v for k, v in bundle.items() if k != "model_hash"}):
        raise ValueError("Recovery model integrity mismatch")
    if bundle.get("schema") != 1 or tuple(bundle.get("symbols", ())) != config.symbols:
        raise ValueError("Recovery model symbols/schema mismatch")
    if (
        bundle.get("horizon") != "scoring_close"
        or bundle.get("method") != "ridge10/session-cluster/shrink0.5/clock-residual-v1"
    ):
        raise ValueError("Unsupported recovery model method/horizon")
    expected_pairs = {(symbol, kind) for symbol in config.symbols for kind in ("momentum", "reversal")}
    pairs = [(m.get("symbol"), m.get("kind")) for m in bundle["models"]]
    if len(pairs) != len(expected_pairs) or set(pairs) != expected_pairs:
        raise ValueError("Recovery model families are missing or duplicated")
    for model in bundle["models"]:
        if (
            len(model["mean"]) != 8
            or len(model["scale"]) != 8
            or len(model["coefficients"]) != 9
            or np.asarray(model["covariance"]).shape != (9, 9)
        ):
            raise ValueError("Invalid recovery model dimensions")
        if (np.asarray(model["scale"]) <= 0).any() or model["sessions"] < 20:
            raise ValueError("Invalid recovery model scaling/coverage")
        arrays = [model["mean"], model["scale"], model["coefficients"], model["covariance"]]
        if not all(np.isfinite(np.asarray(a)).all() for a in arrays):
            raise ValueError("Nonfinite recovery model")
        if np.linalg.eigvalsh(np.asarray(model["covariance"])).min() < -1e-10:
            raise ValueError("Invalid recovery parameter covariance")
    return bundle


def infer(bundle, frame, config, now, manifest):
    verify_bundle(bundle, config)
    if datetime.fromisoformat(bundle["trained_before"]) > now:
        raise ValueError("Recovery model uses future training data")
    frame = (
        frame[frame.timestamp <= pd.Timestamp(now)]
        if frame.attrs.get("recovery_normalized")
        else normalize_bars(frame, now)
    )
    end = manifest.session_close(now.astimezone(NY).date())
    if now >= end:
        return []
    forecasts = []
    for symbol in config.symbols:
        today = frame[(frame.symbol == symbol) & (frame.day == now.astimezone(NY).date())]
        past = frame[(frame.symbol == symbol) & (frame.day < now.astimezone(NY).date())]
        if today.empty or past.empty:
            continue
        at = today.timestamp.iloc[-1].to_pydatetime()
        if not 0 <= (now - at).total_seconds() <= config.max_bar_age:
            continue
        for model in [m for m in bundle["models"] if m["symbol"] == symbol]:
            if pd.Timestamp(model["trained_through"]).date() >= now.astimezone(NY).date():
                raise ValueError("Recovery model includes current/future session labels")
            if (
                now.astimezone(NY).date() - pd.Timestamp(model["trained_through"]).date()
            ).days > config.max_model_age_days:
                continue
            x = features(
                today,
                float(past.close.iloc[-1]),
                model["kind"],
                end.astimezone(NY).hour * 60 + end.astimezone(NY).minute,
            )
            if x is None:
                continue
            design = np.r_[1.0, (x - np.array(model["mean"])) / np.array(model["scale"])]
            mu = float(np.clip(0.5 * (design @ np.array(model["coefficients"])), -0.02, 0.02))
            se = 0.5 * np.sqrt(max(0, float(design @ np.array(model["covariance"]) @ design)))
            conservative = float(np.sign(mu) * max(0, abs(mu) - 1.645 * se))
            forecasts.append(
                {
                    "symbol": symbol,
                    "strategy": model["kind"],
                    "regime": ("trend" if abs(x[4]) > 1.5 * x[5] * np.sqrt(60) else "range_or_uncertain"),
                    "mean_return": mu,
                    "bar_price": float(today.close.iloc[-1]),
                    "parameter_se": float(se),
                    "residual_sd": model.get("residual_sd_by_clock", {}).get(
                        str(now.astimezone(NY).hour * 2 + now.astimezone(NY).minute // 30),
                        model["residual_sd"],
                    ),
                    "conservative_return": conservative,
                    "bar_at": at.isoformat(),
                    "generated_at": now.isoformat(),
                    "horizon_end": manifest.session_close(now.astimezone(NY).date()).isoformat(),
                    "model_hash": bundle["model_hash"],
                    "training_sessions": model["sessions"],
                    "volume_basis": "provider-reported; venue coverage must be verified",
                }
            )
    return forecasts


def shift_scenarios(manifest, scenario, quotes, forecasts, kind):
    """Same residual paths/stresses; add bounded conservative market forecasts."""
    means = {f["symbol"]: f["conservative_return"] for f in forecasts if f["strategy"] == kind}
    residual_sd = {f["symbol"]: f.get("residual_sd", 0) for f in forecasts if f["strategy"] == kind}
    changes = scenario.changes.copy()
    for j, i in enumerate(scenario.ids):
        c = manifest.contract_map[i]
        if c.kind == "STK" and c.symbol in means:
            # Preserve joint draw ordering/correlations; never shrink baseline
            # uncertainty below the empirically observed matched-clock residual.
            current = changes[:, j]
            centered = current - current.mean()
            sd = float(centered.std())
            scale = max(1.0, quotes[i].spot * residual_sd.get(c.symbol, 0) / sd) if sd else 1.0
            changes[:, j] = current.mean() + centered * scale + quotes[i].spot * means[c.symbol]
    version = digest({"base": scenario.identifier, "forecasts": forecasts, "strategy": kind})
    return replace(
        scenario,
        changes=changes,
        identifier=version,
        source=scenario.source + f"; recovery {kind} conservative close forecast",
    )


def rebase_forecasts(forecasts, quotes, manifest):
    forecasts = [dict(f) for f in forecasts]
    for forecast in forecasts:
        stock = next(
            (c for c in manifest.contracts if c.kind == "STK" and c.symbol == forecast["symbol"]),
            None,
        )
        if stock is None or stock.con_id not in quotes:
            continue
        current = quotes[stock.con_id].spot
        bar = forecast["bar_price"]
        if abs(current / bar - 1) > 0.02:
            raise ValueError("Recovery bar/live-price discontinuity: verify split, adjustment, or feed")
        forecast["bar_mean_return"] = forecast["mean_return"]
        forecast["mean_return"] = bar * (1 + forecast["mean_return"]) / current - 1
        forecast["parameter_se"] *= bar / current
        forecast["residual_sd"] *= bar / current
        mu, se = forecast["mean_return"], forecast["parameter_se"]
        forecast["conservative_return"] = float(np.sign(mu) * max(0, abs(mu) - 1.645 * se))
        forecast["current_price"] = current
    return forecasts


def promotion_errors(config, bundle, evidence, manifest):
    errors = []
    metrics = ("recovery_mean_score", "hedge_mean_score", "recovery_p90", "hedge_p90")

    def valid_metric(key):
        value = evidence.get(key)
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and np.isfinite(value)
            and value >= 0
        )

    if any(key not in evidence for key in metrics):
        errors.append("Combined contest score results have not been recorded")
    if any(key in evidence and not valid_metric(key) for key in metrics):
        errors.append("Finite nonnegative contest metrics required; recorded results are invalid")
    if evidence.get("model_hash") != bundle.get("model_hash"):
        errors.append("Evidence does not match frozen model")
    if evidence.get("contest_id") != manifest.contest_id:
        errors.append("Evidence does not match contest mandates/calendar")
    if evidence.get("config_hash") != manifest.config_hash:
        errors.append("Evidence does not match the active risk/policy configuration")
    if evidence.get("evidence_class") != "quote_based_combined_contest_replay":
        errors.append("Combined quote-based contest replay required; bars are provisional")
    windows = evidence.get("independent_test_windows", 0)
    if not isinstance(windows, int) or isinstance(windows, bool) or windows < 20:
        errors.append("At least 20 independent held-out contest windows required")
    if all(valid_metric(k) for k in metrics[:2]) and not (
        evidence["recovery_mean_score"] < evidence["hedge_mean_score"]
    ):
        errors.append("Held-out mean contest score did not improve")
    if all(valid_metric(k) for k in metrics[2:]) and evidence["recovery_p90"] > evidence["hedge_p90"]:
        errors.append("Held-out tail contest score deteriorated")
    if not evidence.get("shadow_passed") or not evidence.get("operational_fault_tests_passed"):
        errors.append("Forward shadow and fault tests required")
    if evidence.get("synthetic") is not False or not evidence.get("provenance"):
        errors.append("Empirical replay provenance required")
    if evidence.get("forecast_trained_before_test") is not True:
        errors.append("Chronological frozen-model evaluation required")
    for field, message in (
        ("bar_timestamp_verified", "Verified bar availability convention required"),
        ("volume_coverage_verified", "Feature volume coverage verification required"),
        ("cash_benchmark_verified", "Sourced historical cash benchmark required"),
        ("execution_path_validated", "Recovery execution paths must be validated"),
    ):
        if evidence.get(field) is not True:
            errors.append(message)
    return errors


class RecoveryController:
    def __init__(self, manifest, store):
        self.manifest, self.store = manifest, store
        self.config = RecoveryConfig.model_validate(manifest.recovery)
        self.mode = "shadow" if self.config.mode == "paper" else self.config.mode
        self.latest = {"mode": self.mode, "status": "Not evaluated", "forecasts": [], "candidates": []}
        self._promotion_cache = None

    def promotion_status(self):
        """Cached dashboard explanation only; execution rechecks uncached evidence."""
        paths = (Path(self.config.model_path), Path(self.config.evidence_path))
        try:
            stamps = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
            if self._promotion_cache and self._promotion_cache[0] == stamps:
                return self._promotion_cache[1]
            bundle = verify_bundle(json.loads(paths[0].read_text()), self.config)
            evidence = json.loads(paths[1].read_text())
            blockers = promotion_errors(self.config, bundle, evidence, self.manifest)
            count = evidence.get("independent_test_windows", 0)
            status = {
                "eligible": not blockers,
                "status": "Validation passed" if not blockers else "Paper recovery unavailable: validation incomplete",
                "blockers": blockers,
                "evidence_class": evidence.get("evidence_class", "unavailable"),
                "independent_test_windows": count if isinstance(count, int) and not isinstance(count, bool) else 0,
            }
            self._promotion_cache = (stamps, status)
            return status
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return {
                "eligible": False,
                "status": "Paper recovery unavailable: evidence missing or invalid",
                "blockers": ["Recovery approval unavailable: " + str(exc)],
                "evidence_class": "unavailable",
                "independent_test_windows": 0,
            }

    def data(self):
        historical = pd.read_parquet(self.config.bars_path)
        live_path = Path(self.config.live_bars_path)
        if live_path.exists():
            live = pd.read_csv(live_path)
            historical = pd.concat([historical, live], ignore_index=True)
            historical["timestamp"] = pd.to_datetime(historical.timestamp, utc=True)
            historical = historical.drop_duplicates(["timestamp", "symbol"], keep="last")
        bundle = verify_bundle(json.loads(Path(self.config.model_path).read_text()), self.config)
        return bundle, historical

    def set_mode(self, mode):
        if mode not in ("off", "shadow", "paper"):
            raise ValueError("Unknown recovery mode")
        if mode != "off":
            approved = {
                c.symbol
                for c in self.manifest.contracts
                if c.kind == "STK" and c.con_id in self.manifest.allowlist
            }
            if not set(self.config.symbols).issubset(approved):
                raise ValueError("Recovery symbols require qualified, approved stock contracts")
        if mode == "paper":
            bundle, _ = self.data()
            evidence = json.loads(Path(self.config.evidence_path).read_text())
            errors = promotion_errors(self.config, bundle, evidence, self.manifest)
            if errors:
                raise ValueError("Paper recovery unavailable; recovery mode unchanged. " + "; ".join(errors))
        self.mode = mode
        self.store.record("recovery_control", self.manifest.account, {"mode": mode})
        self.latest["mode"] = mode
        return self.latest

    def observe_close(self, quotes, now):
        day = now.astimezone(NY).date()
        if day not in (self.manifest.day_zero, *self.manifest.scoring_sessions):
            return
        close = self.manifest.session_close(day)
        if not 0 <= (now - close).total_seconds() <= 180:
            return
        existing = [
            r
            for r in self.store.records("recovery_close", self.manifest.account, 100)
            if r.get("day") == str(day)
        ]
        if existing:
            return
        marks = {
            str(i): str(q.mid)
            for i, q in quotes.items()
            if self.manifest.contract_map[i].kind == "STK"
            and abs((q.observed_at - close).total_seconds()) <= 60
        }
        if marks:
            self.store.record(
                "recovery_close",
                self.manifest.account,
                {"day": str(day), "marks": marks, "source": "timestamped market-book NAV proxy"},
            )

    def paper_errors(self, model_hash=None):
        try:
            bundle, _ = self.data()
            errors = promotion_errors(
                self.config, bundle, json.loads(Path(self.config.evidence_path).read_text()), self.manifest
            )
            if model_hash is not None and model_hash != bundle["model_hash"]:
                errors.append("Frozen recovery model changed; regenerate forecast")
            return errors
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ["Recovery approval unavailable: " + str(exc)]

    def execution_errors(
        self, snapshot, quotes, scenarios, quantities, reference, costs, now, model_hash=None
    ):
        errors = recovery_budget_errors(
            self.manifest, self.config, snapshot, quotes, scenarios, quantities, reference, costs
        )
        if self.mode != "paper":
            errors.append("Recovery execution mode is not enabled")
        errors.extend(self.paper_errors(model_hash))
        pnl = attributed_trading_pnl(self.store, self.manifest, quotes, now)
        budget = min(float(self.manifest.initial_nav), float(snapshot.nav))
        if pnl["daily_trading_pnl"] <= -self.config.daily_loss_stop * budget:
            if any(
                abs(quantities.get(i, 0) - reference.get(i, 0))
                > abs(snapshot.quantities().get(i, 0) - reference.get(i, 0))
                for i in scenarios.ids
            ):
                errors.append("Recovery daily loss stop blocks new exposure")
        return errors

    def evaluate(self, optimizer, snapshot, quotes, scenarios, baseline, previous, estimate, now):
        result = {
            "mode": self.mode,
            "at": now.isoformat(),
            "status": "OFF",
            "forecasts": [],
            "candidates": [],
        }
        if self.mode == "off":
            self.latest = result
            return None, None, result
        try:
            pnl = attributed_trading_pnl(self.store, self.manifest, quotes, now)
            budget = min(float(self.manifest.initial_nav), float(snapshot.nav))
            result.update(
                pnl=pnl,
                stress_budget=self.config.incremental_stress * budget,
                loss_stop=self.config.daily_loss_stop * budget,
            )
            if pnl["daily_trading_pnl"] <= -self.config.daily_loss_stop * budget:
                raise ValueError("Recovery daily trading loss stop reached; no new entries")
            bundle, bars = self.data()
            forecasts = infer(bundle, bars, self.config, now, self.manifest)
            forecasts = rebase_forecasts(forecasts, quotes, self.manifest)
            result.update(forecasts=forecasts, model_hash=bundle["model_hash"])
            if not forecasts:
                raise ValueError("No complete fresh bars with eligible forecast history")
            if abs(estimate - float(previous)) <= self.config.target_band:
                result["status"] = "Target band: no new recovery entries"
                self.latest = result
                return None, None, result
            if self.mode == "paper":
                evidence = json.loads(Path(self.config.evidence_path).read_text())
                errors = promotion_errors(self.config, bundle, evidence, self.manifest)
                if errors:
                    self.mode = "shadow"
                    raise ValueError("Promotion invalidated: " + "; ".join(errors))
            candidates = []
            for kind in ("momentum", "reversal"):
                if not any(f["strategy"] == kind and f["conservative_return"] for f in forecasts):
                    continue
                shifted = shift_scenarios(self.manifest, scenarios, quotes, forecasts, kind)
                target = optimizer.solve(
                    snapshot,
                    quotes,
                    shifted,
                    previous,
                    estimate,
                    recovery_reference=baseline.quantities,
                    recovery_symbols=self.config.symbols,
                    recovery_stress=self.config.incremental_stress,
                    recovery_gross=self.config.gross_notional,
                )
                # Compare to the base hedge under the SAME market forecast.
                reference = optimizer.evaluate_target(snapshot, quotes, shifted, baseline, previous, estimate)
                improvement = reference.expected_score - target.expected_score
                threshold = max(
                    self.manifest.policy.score_threshold,
                    self.manifest.policy.uncertainty_multiplier * target.uncertainty,
                )
                from .risk import RiskGate

                gate = RiskGate(self.manifest, demo=snapshot.source == "synthetic").validate(
                    snapshot, quotes, shifted, target.quantities, now, 2 * target.estimated_cost
                )
                extra = recovery_budget_errors(
                    self.manifest,
                    self.config,
                    snapshot,
                    quotes,
                    shifted,
                    target.quantities,
                    baseline.quantities,
                    2 * target.estimated_cost,
                )
                candidate = {
                    **target.model_dump(mode="json"),
                    "risk_passed": gate.passed and not extra,
                    "risk_reasons": [*gate.reasons, *extra],
                    "strategy": kind,
                    "baseline_distance": reference.expected_score,
                    "improvement": improvement,
                    "threshold": threshold,
                    "eligible": target.status == "OPTIMAL"
                    and improvement > threshold
                    and gate.passed
                    and not extra,
                }
                result["candidates"].append(candidate)
                if candidate["eligible"]:
                    candidates.append((target.expected_score, kind, target, shifted))
            result["status"] = (
                "Candidate ready" if candidates else "No forecast-supported qualifying recovery trade"
            )
            self.latest = result
            if not candidates:
                return None, None, result
            _, kind, target, shifted = min(candidates, key=lambda row: (row[0], row[1]))
            result["selected_strategy"] = kind
            result["reference_quantities"] = baseline.quantities
            return target, shifted, result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["status"] = "Blocked: " + str(exc)
            result["mode"] = self.mode
            self.latest = result
            return None, None, result


def recovery_budget_errors(manifest, config, snapshot, quotes, scenarios, quantities, reference, costs=0):
    """Independent incremental gate; used again for every actual execution slice."""
    budget = min(float(manifest.initial_nav), float(snapshot.nav))
    ids = scenarios.ids
    delta = np.array([quantities.get(i, 0) - reference.get(i, 0) for i in ids])
    loss = max(0.0, float(-np.min(scenarios.stress @ delta)) + costs)
    gross = sum(
        abs(quantities.get(i, 0) - reference.get(i, 0)) * quotes[i].spot
        for i in ids
        if manifest.contract_map[i].kind == "STK" and manifest.contract_map[i].symbol in config.symbols
    )
    errors = []
    if loss > config.incremental_stress * budget + 0.01:
        errors.append("Recovery incremental stress budget exceeded")
    if gross > config.gross_notional * budget + 0.01:
        errors.append("Recovery gross budget exceeded")
    return errors


def attributed_trading_pnl(store, manifest, quotes, now):
    """Rebuild strategy attribution from corrected IBKR fills; not simulated cash."""
    from decimal import Decimal

    decisions = {r["id"] for r in store.records("decision", manifest.account, 10000) if r.get("recovery")}
    intentions = {r["broker_order_id"]: r for r in store.intents(manifest.account) if r["broker_order_id"]}
    quantities, cash = {}, Decimal(0)
    prior_quantities, prior_cash = {}, Decimal(0)
    day_start = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0)
    provisional = False
    for fill in sorted(store.effective_fills(manifest.account), key=lambda r: r["at"]):
        intention = intentions.get(fill["order_id"])
        if not intention or intention.get("purpose") == "burn":
            continue  # closing-burner round trips never touch recovery inventory or P&L
        i, amount = fill["con_id"], fill["signed_quantity"]
        recovery_fill = intention["decision_id"] in decisions
        owned = quantities.get(i, 0)
        attributed = (
            amount
            if recovery_fill
            else (int(np.sign(amount) * min(abs(amount), abs(owned))) if owned * amount < 0 else 0)
        )
        if not attributed:
            continue
        fee = fill["commission"]
        if fee is None:
            provisional = True
            fee = max(
                Decimal(str(manifest.policy.minimum_commission)),
                abs(amount) * Decimal(str(manifest.policy.commission_share)),
            )
        quantities[i] = owned + attributed
        cash -= Decimal(attributed) * fill["price"] + fee * Decimal(abs(attributed)) / Decimal(abs(amount))
        fill_at = fill["at"]
        if fill_at.tzinfo is None:
            fill_at = fill_at.replace(tzinfo=timezone.utc)  # SQLite stores the known UTC column naive.
        if fill_at < day_start:
            prior_cash, prior_quantities = cash, dict(quantities)
    missing = [i for i, q in quantities.items() if q and i not in quotes]
    if missing:
        raise ValueError("Recovery marks unavailable")
    total = cash + sum(Decimal(q) * Decimal(str(quotes[i].mid)) for i, q in quantities.items() if q)
    day = str(now.astimezone(NY).date())
    entries = [
        r
        for r in store.records("recovery_day_baseline", manifest.account, 1000)
        if r.get("day") == day and r.get("contest_id") == manifest.contest_id
    ]
    if not entries:
        # On first activation with no attributed inventory baseline is zero.
        # Persist across switches/restarts so a loss stop cannot be reset.
        if any(prior_quantities.values()):
            closes = [
                r for r in store.records("recovery_close", manifest.account, 100) if r.get("day", "") < day
            ]
            if not closes:
                raise ValueError("Prior closing recovery marks required for daily loss stop")
            marks = max(closes, key=lambda r: r["day"])["marks"]
            if not all(str(i) in marks for i, q in prior_quantities.items() if q):
                raise ValueError("Incomplete prior closing recovery marks")
            initial = prior_cash + sum(
                Decimal(q) * Decimal(marks[str(i)]) for i, q in prior_quantities.items() if q
            )
        else:
            initial = prior_cash
        store.record(
            "recovery_day_baseline",
            manifest.account,
            {"day": day, "contest_id": manifest.contest_id, "pnl": str(initial)},
        )
    else:
        initial = Decimal(entries[0]["pnl"])
    return {
        "daily_trading_pnl": float(total - initial),
        "total_trading_pnl": float(total),
        "quantities": quantities,
        "commissions_provisional": provisional,
        "financing_attribution": "Excluded: broker financing remains in combined NAV",
    }

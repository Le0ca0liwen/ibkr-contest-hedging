from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from hedge.config import Manifest
from hedge.models import Frozen, PortfolioSnapshot, Quote
from hedge.scenarios import ScenarioSet


class Settings(Frozen):
    source_url: str = "http://127.0.0.1:8765"
    symbols: tuple[str, ...] = ("BA", "SPY", "QQQ", "XLI", "TLT")
    quantities: tuple[int, ...] = (1, 5, 10, 25, 50, 100)
    # None means a fresh, surplus-sized allowance, never an unlimited loss allowance.
    daily_expense_budget: Decimal | None = Field(default=None, gt=0)
    daily_order_budget: int = Field(default=2000, ge=2, le=10000)
    buffer: float = Field(default=25, ge=1)
    target_estimate_buffer: float = Field(default=25, ge=0)
    start_threshold: float = Field(default=10, gt=0)
    stop_threshold: float = Field(default=2, ge=0)
    minimum_improvement: float = Field(default=0.25, gt=0)
    maximum_overshoot_probability: float = Field(default=0.01, ge=0, le=0.05)
    monitor_seconds: float = Field(default=5, ge=1, le=60)
    assumed_cycle_seconds: float = Field(default=15, ge=5, le=120)
    entry_window_minutes: int = Field(default=30, ge=3, le=30)
    entry_cutoff_minutes: int = Field(default=2, ge=2, le=10)
    scenario_age_seconds: int = Field(default=20, ge=1, le=60)

    @model_validator(mode="after")
    def valid(self):
        url = urlsplit(self.source_url)
        if (
            url.scheme != "http"
            or url.hostname not in ("127.0.0.1", "localhost")
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Source must be a plain localhost HTTP URL")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Explicit unique stock/ETF symbols required")
        if not self.quantities or any(q <= 0 or q > 100 for q in self.quantities):
            raise ValueError("Research slices must be between one and 100 shares")
        if (
            self.stop_threshold >= self.start_threshold
            or self.entry_cutoff_minutes >= self.entry_window_minutes
        ):
            raise ValueError("Contradictory hysteresis or entry window")
        return self


class ScenarioPayload(Frozen):
    ids: tuple[int, ...]
    changes: tuple[tuple[float, ...], ...]
    weights: tuple[float, ...]
    stress: tuple[tuple[float, ...], ...]
    calendar_days: float = Field(ge=0)
    source: str
    identifier: str
    # The main app's approved event caution in this forecast set (symbol -> 1.25/1.5); also in source.
    event_caution: dict[str, float] = Field(default_factory=dict)

    def array(self):
        import numpy as np

        return ScenarioSet(
            self.ids,
            np.asarray(self.changes),
            np.asarray(self.weights),
            np.asarray(self.stress),
            None,
            self.calendar_days,
            self.source,
            self.identifier,
            event_caution=dict(self.event_caution),
        )


class Input(Frozen):
    protocol: str = "surplus-research-v1"
    generated_at: datetime
    manifest: Manifest
    snapshot: PortfolioSnapshot
    quotes: dict[int, Quote]
    scenarios: ScenarioPayload | None
    scenario_at: datetime | None
    scenario_snapshot_id: str | None
    scenario_revision: int | None
    broker_revision: int = Field(ge=0)
    baseline_nav: Decimal | None = Field(gt=0)
    nav_estimate: float | None
    target: Decimal | None
    target_estimated: bool
    forecast_close: datetime | None
    main_mode: str
    execution_busy: bool
    config_hash: str
    snapshot_id: str

    @model_validator(mode="after")
    def identities(self):
        if self.protocol != "surplus-research-v1":
            raise ValueError("Unsupported snapshot protocol")
        if self.snapshot.account != self.manifest.account or self.snapshot_id != self.snapshot.version:
            raise ValueError("Snapshot/account identity mismatch")
        if self.config_hash != self.manifest.config_hash:
            raise ValueError("Configuration hash mismatch")
        if len({p.con_id for p in self.snapshot.positions}) != len(self.snapshot.positions):
            raise ValueError("Duplicate position identities")
        if any(i != q.con_id for i, q in self.quotes.items()):
            raise ValueError("Quote contract identity mismatch")
        for at in (
            self.generated_at,
            self.scenario_at,
            self.forecast_close,
            self.snapshot.at,
            self.snapshot.account_at,
            *(q.greek_at for q in self.quotes.values()),
        ):
            if at is not None and at.tzinfo is None:
                raise ValueError("Timezone-aware evidence required")
        if self.scenarios is not None:
            if (
                len(set(self.scenarios.ids)) != len(self.scenarios.ids)
                or not self.scenarios.changes
                or not self.scenarios.stress
                or any(len(row) != len(self.scenarios.ids) for row in self.scenarios.stress)
            ):
                raise ValueError("Complete unique scenario dimensions required")
            self.scenarios.array()  # Validate shape, finite values and probability mass.
            if not set(self.scenarios.ids).issubset(self.manifest.contract_map):
                raise ValueError("Unknown scenario contract")
        return self

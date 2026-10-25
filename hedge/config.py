from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import yaml
from pydantic import Field, field_validator, model_validator

from .models import Contract, Dividend, Frozen, digest

NY = ZoneInfo("America/New_York")
# Post-market session after the regular close (16:00-20:00 ET). In the SPCX/RKLB/SPY contest its
# trading and NAV changes count (operator instruction, Oct 22): the official close is read after it
# and SPCX/RKLB (and other allowlisted) shares keep hedging through it. Options never trade in it.
EXTENDED_HOURS = timedelta(hours=4)
# Pre-market session before the regular open (04:00-09:30 ET): shares hedge through it as well
# (operator instruction, Oct 22). The day's NAV runs from the previous scored close (20:00 ET).
PRE_MARKET = timedelta(hours=5, minutes=30)
# Options on these ETFs trade until 16:15 ET, and IBKR's statement marks them then (SPY on the
# Oct 22 statement: between the 16:00 and 16:20 midpoints); every other option closes at 16:00.
LATE_OPTION_SYMBOLS = frozenset({"SPY", "QQQ", "IWM", "DIA"})
LATE_OPTION_CLOSE = timedelta(minutes=15)
# Implied vol rises when spot falls (leverage effect); broad index ETFs show it most strongly.
BROAD_INDEX_ETFS = frozenset({"SPY", "IVV", "VOO", "SPLG", "QQQ", "IWM", "DIA"})
INDEX_SPOT_VOL_CORRELATION = -0.7
SINGLE_NAME_SPOT_VOL_CORRELATION = -0.4


class RiskLimits(Frozen):
    stress_loss: float = Field(default=0.03, gt=0, le=0.03)
    liquidity_buffer: float = Field(default=0.25, ge=0.25, lt=1)
    gross_notional: float = Field(default=3, gt=0, le=3)
    stock_notional: float = Field(default=0.5, gt=0, le=0.5)
    residual_delta: float = Field(default=0.1, gt=0, le=0.2)
    contracts_per_option: int = Field(default=50, gt=0, le=50)
    option_names: int = Field(default=6, gt=0, le=6)
    stock_quote_age: int = Field(default=5, gt=0)
    option_quote_age: int = Field(default=60, gt=0)
    # Moomoo pushes a book only when it changes. A held option that is not being traded may keep
    # an unchanged (quiet) book this long before it blocks all hedging; traded options stay strict.
    held_option_quote_age: int = Field(default=300, ge=60, le=900)
    greek_age: int = Field(default=60, gt=0)
    account_age: int = Field(default=180, gt=0)
    rate_stress_shift: float = Field(default=0.01, ge=0.005, le=0.05)


class TradingPolicy(Frozen):
    risk_seconds: int = Field(default=5, ge=1)
    stock_seconds: int = Field(default=60, ge=15)
    option_seconds: int = Field(default=900, ge=60)
    reconcile_seconds: int = Field(default=60, ge=10)
    score_threshold: float = Field(default=100, ge=0)
    uncertainty_multiplier: float = Field(default=1.645, ge=0)
    option_cutoff_minutes: int = Field(default=15, ge=2)
    stock_cutoff_minutes: int = Field(default=2, ge=1)
    solver_seconds: float = Field(default=10, gt=0, le=10)
    # Certified selection band ($) for the primary stage: the accepted portfolio's modeled score is
    # within this of the true optimum (the solver stops at a quarter of it). $1 proves to the cent,
    # which can exhaust the 10 s budget on a contest-sized universe; the trade threshold is >= $100.
    primary_band: float = Field(default=1, ge=0.04, le=50)
    ranking_seconds: float = Field(default=1, gt=0, le=5)
    ranking_nodes: int = Field(default=32, ge=1, le=10000)
    commission_share: float = Field(default=0.005, ge=0)
    commission_option: float = Field(default=0.65, ge=0)
    minimum_commission: float = Field(default=1, ge=0)
    slippage_spread_fraction: float = Field(default=0.1, ge=0)
    borrow_rate: float = Field(default=0.05, ge=0)
    financing_rate: float = Field(default=0.06, ge=0)
    cash_rate: float = Field(default=0, ge=0)
    cash_rate_source: str | None = None
    cash_rate_status: Literal["unconfigured", "modeled", "verified"] = "unconfigured"
    cash_interest_free_balance: float = Field(default=10000, ge=0)
    cash_day_count: Literal[360, 365] = 360
    seed: int = 17
    history_sessions: Literal[20, 60, 120] = 60
    # True: scored costs sit inside |closing gap - costs|, so a NAV surplus above target is partly
    # spent down by trading (burn). False: |closing gap| + costs, a separate additive penalty.
    burn_surplus: bool = True
    # Weight of the close -> next scored session's open + 15 min gap (decay and moves the app
    # cannot trade against overnight) relative to the closing gap; 0 disables the term.
    overnight_weight: float = Field(default=0.5, ge=0, le=2)

    @model_validator(mode="after")
    def interest_provenance(self):
        if self.cash_rate and (self.cash_rate_status == "unconfigured" or not self.cash_rate_source):
            raise ValueError("Positive cash-interest forecasts require rate status and provenance")
        return self


class ClosingCapturePolicy(Frozen):
    enabled: bool = True
    window_seconds: int = Field(default=180, ge=15, le=300)
    retry_seconds: int = Field(default=15, ge=5, le=60)
    # One more API observation this long after the bell, once SPY options have marked at their
    # close (15 minutes later) and account values have refreshed; 0 disables it.
    late_capture_minutes: int = Field(default=20, ge=0, le=60)
    # After 16:00 IBKR may mark stocks at extended-hours prices, so a post-close reading that moves
    # more than this fraction of initial NAV from the bell value is not used (statement required).
    late_capture_max_delta: float = Field(default=0.0015, gt=0, le=0.05)

    @model_validator(mode="after")
    def late_after_options_close(self):
        # Options close 15 minutes after the bell and account values refresh about every 3 minutes.
        if 0 < self.late_capture_minutes < 18:
            raise ValueError(
                "Post-close NAV capture must follow the options close and a refresh (18+ minutes)"
            )
        return self


class ExecutionPolicy(Frozen):
    stock_slice: int = Field(default=100, ge=1, le=1000)
    option_slice: int = Field(default=1, ge=1, le=10)
    minimum_stock_notional: float = Field(default=1000, ge=0)
    minimum_slice_improvement: float = Field(default=1, ge=0)
    stock_spread_absolute: float = Field(default=0.20, gt=0)
    stock_spread_relative: float = Field(default=0.002, gt=0, le=0.05)
    option_spread_absolute: float = Field(default=0.50, gt=0)
    option_spread_relative: float = Field(default=0.15, gt=0, le=0.50)
    stock_wait_seconds: int = Field(default=15, ge=5, le=120)
    option_wait_seconds: int = Field(default=60, ge=10, le=300)
    setup_wait_seconds: int = Field(default=120, ge=30, le=600)
    stock_lifetime_seconds: int = Field(default=60, ge=30, le=300)
    option_lifetime_seconds: int = Field(default=180, ge=60, le=900)
    setup_lifetime_seconds: int = Field(default=300, ge=120, le=1800)
    setup_opening_wait_minutes: int = Field(default=5, ge=0, le=60)
    setup_cutoff_minutes: int = Field(default=1, ge=1, le=30)
    urgent_market_stocks: bool = True
    market_quote_age: float = Field(default=1, gt=0, le=2)
    market_spread_absolute: float = Field(default=0.02, gt=0, le=0.05)
    market_depth_multiple: int = Field(default=5, ge=2)
    market_slippage_bps: float = Field(default=5, gt=0, le=20)


class BurnerPolicy(Frozen):
    """Closing surplus burner: deliberate, bounded spending of a NAV surplus near the close.

    Paper mode trades only allowlisted stocks (and optionally non-mandated options) in strictly
    sequential buy-then-sell IOC round trips. See docs/CLOSING_BURNER.md.
    """

    mode: Literal["off", "shadow", "paper"] = "off"
    # Provenance that deliberate expense through ordinary trades is admissible (required for paper).
    admissibility_source: str = ""
    symbols: tuple[str, ...] = ()
    quantities: tuple[int, ...] = (1, 5, 10, 25, 50, 100, 200)
    max_shares: int = Field(default=200, ge=1, le=10000)
    option_burn: bool = False
    option_max_contracts: int = Field(default=5, ge=1, le=50)
    option_max_spread: float = Field(default=0.20, gt=0, le=5)
    window_minutes: int = Field(default=20, ge=2, le=120)
    stock_entry_cutoff_minutes: int = Field(default=4, ge=1, le=60)
    option_entry_cutoff_minutes: int = Field(default=17, ge=1, le=120)
    start_threshold: float = Field(default=25, ge=0)
    stop_threshold: float = Field(default=5, ge=0)
    # Spend toward this quantile of the closing gap (0.5 = the L1-optimal median). Lower keeps a
    # margin for forecast error: a burn cannot be undone if NAV falls before the close.
    spend_quantile: float = Field(default=0.5, ge=0.05, le=0.5)
    # After the close the gap is uncertain only by each option's statement price within its closing
    # bid/ask. worst_case spends only what stays above target with every option at its least
    # favorable side; quantile spends toward spend_quantile of the samples around the predicted price;
    # prediction spends down to the predicted statement NAV less post_close_margin (worst_case for
    # any held option priced without the OCC rebuild).
    post_close_basis: Literal["worst_case", "quantile", "prediction"] = "worst_case"
    post_close_margin: float = Field(default=50, ge=0)
    # Act only on IBKR's 1-second P&L stream rolled from a settled NetLiquidation (never on fill
    # attribution, whose base can be a NetLiquidation push sent mid-fill), and wait this long after
    # each cycle so its fills and commissions reach the stream before the next decision.
    require_pnl_stream: bool = False
    settle_seconds: float = Field(default=0, ge=0, le=120)
    bias: float = 0.0
    bias_source: str = ""
    jit_headroom: float = Field(default=0.5, ge=0, le=1)
    jit_reserve_minutes: float = Field(default=1, ge=0, le=60)
    assumed_cycle_seconds: float = Field(default=20, gt=0, le=600)
    minimum_improvement: float = Field(default=0.25, ge=0)
    daily_expense_cap: Decimal | None = Field(default=None, gt=0)
    max_cycles_per_day: int = Field(default=300, ge=1, le=5000)
    max_exit_attempts: int = Field(default=4, ge=1, le=20)
    exit_collar_ticks: tuple[int, ...] = (0, 1, 2, 5)
    quote_age_seconds: float = Field(default=3, gt=0, le=60)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value):
        result = tuple(s.strip().upper() for s in value)
        if "" in result or len(set(result)) != len(result):
            raise ValueError("Burner symbols must be distinct and nonempty")
        return result

    @model_validator(mode="after")
    def coherent(self):
        if self.mode == "paper" and not self.admissibility_source.strip():
            raise ValueError("Paper burning requires a recorded admissibility_source")
        if not self.quantities or any(q <= 0 for q in self.quantities):
            raise ValueError("Burner quantities must be positive")
        if tuple(sorted(set(self.quantities))) != self.quantities:
            raise ValueError("Burner quantities must be sorted and unique")
        if self.stop_threshold >= self.start_threshold:
            raise ValueError("Burner stop_threshold must be below start_threshold")
        if self.bias and not self.bias_source.strip():
            raise ValueError("A nonzero burner bias requires its calibration source")
        if not self.exit_collar_ticks or any(t < 0 for t in self.exit_collar_ticks):
            raise ValueError("Exit collars must be nonnegative tick counts")
        if list(self.exit_collar_ticks) != sorted(self.exit_collar_ticks):
            raise ValueError("Exit collars must widen (non-decreasing ticks)")
        if self.window_minutes <= self.stock_entry_cutoff_minutes:
            raise ValueError("Burner window must open before the stock entry cutoff")
        if self.option_burn and self.window_minutes <= self.option_entry_cutoff_minutes:
            raise ValueError("Option burning needs the window to open before the option entry cutoff")
        return self


class Manifest(Frozen):
    name: str
    beta_benchmark: str = Field(default="SPY", min_length=1)
    account: str
    environment: Literal["paper"] = "paper"
    day_zero: date
    mandatory_purchase_session: date | None = None
    scoring_sessions: tuple[date, ...]
    valuation_convention: str
    report_source: str
    scoring_rule: Literal[
        "raw_nav_absolute",
        "pending_interest_adjustment",
        "interest_benchmark",
        "calendar_interest_benchmark",
        "actual_daily_cash_rate",
    ] = "raw_nav_absolute"
    scoring_annual_rate: Decimal | None = Field(default=None, ge=0, le=1)
    scoring_daily_rates: dict[date, Decimal] = Field(default_factory=dict)
    scoring_forecast_daily_rate: Decimal | None = Field(default=None, ge=0, le=1)
    scoring_forecast_source: str = ""
    market_data_provider: Literal["ibkr", "moomoo"] = "ibkr"
    # Moomoo has no short-borrow data. Operator-verified shortable shares per symbol (e.g. from
    # IBKR's Short Stock Availability tool) let the optimizer short; empty means no new shorts.
    stock_borrow: dict[str, int] = Field(default_factory=dict)
    stock_borrow_source: str = ""
    daily_stock_history: str | None = None
    # Per-symbol correlation of scenario IV shocks with same-symbol spot shocks (overrides defaults).
    spot_vol_correlation: dict[str, Annotated[float, Field(ge=-0.95, le=0.5)]] = Field(default_factory=dict)
    closing_capture: ClosingCapturePolicy = ClosingCapturePolicy()
    execution: ExecutionPolicy = ExecutionPolicy()
    recovery: dict = Field(default_factory=dict)
    initial_nav: Decimal = Field(default=Decimal("1000000"), gt=0)
    contracts: tuple[Contract, ...]
    contest_rules: Literal["practice", "spcx_rklb_spy"] = "practice"
    contest_option_expiry: date | None = None
    mandatory: dict[int, int]
    # "open" derives the allowlist: every defined, non-mandatory, non-forbidden contract.
    universe: Literal["allowlist", "open"] = "allowlist"
    allowlist: tuple[int, ...] = ()
    dividends: dict[str, tuple[Dividend, ...]] = Field(default_factory=dict)
    risk_free_rate: float = 0.04
    dividend_yields: dict[str, float] = Field(default_factory=dict)
    risk: RiskLimits = RiskLimits()
    policy: TradingPolicy = TradingPolicy()
    burner: BurnerPolicy = BurnerPolicy()
    # "paper": after launch the service starts paper trading by itself once the mandated book is
    # held and every paper activation check passes (the same checks as "Enable paper trading").
    # The dashboard switch overrides it; any operator control cancels a pending automatic start.
    autostart: Literal["off", "paper"] = "off"
    gateway_host: Literal["127.0.0.1"] = "127.0.0.1"
    gateway_port: Literal[4002, 7497] = 4002
    client_id: int = Field(default=71, gt=0)
    confirmed: bool = False
    strategy: Literal["dynamic_l1", "delta", "greek", "min_variance", "historical_l1", "two_close"] = (
        "dynamic_l1"
    )

    @field_validator("spot_vol_correlation")
    @classmethod
    def normalize_spot_vol(cls, value):
        result = {k.strip().upper(): v for k, v in value.items()}
        if "" in result or len(result) != len(value):
            raise ValueError("Spot-vol correlation overrides need distinct nonempty symbols")
        return result

    @model_validator(mode="after")
    def validate_contest(self):
        if self.recovery:
            from .recovery import RecoveryConfig

            RecoveryConfig.model_validate(self.recovery)
        if any(
            day not in self.scoring_sessions or not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1)
            for day, rate in self.scoring_daily_rates.items()
        ):
            raise ValueError(
                "Daily benchmark rates require scored sessions and finite fractional rates between 0 and 1"
            )
        unknown = set(self.spot_vol_correlation) - {c.symbol.upper() for c in self.contracts}
        if unknown:
            raise ValueError(f"spot_vol_correlation symbols not in contracts: {sorted(unknown)}")
        if self.stock_borrow and (
            not self.stock_borrow_source.strip() or any(v < 0 for v in self.stock_borrow.values())
        ):
            raise ValueError("Stock borrow allowances need nonnegative shares and a recorded source")
        if (
            self.scoring_rule in ("interest_benchmark", "calendar_interest_benchmark")
            and self.scoring_annual_rate is None
        ):
            raise ValueError("Instructor's annual benchmark rate is required")
        if self.confirmed and self.scoring_rule == "pending_interest_adjustment":
            raise ValueError(
                "Instructor's interest-adjusted scoring formula is pending; configuration cannot activate"
            )
        if not self.account.startswith("DU"):
            raise ValueError("Only a configured DU paper account is supported")
        if not self.scoring_sessions or tuple(sorted(set(self.scoring_sessions))) != self.scoring_sessions:
            raise ValueError("Scoring sessions must be nonempty, sorted, and unique")
        if self.day_zero >= self.scoring_sessions[0]:
            raise ValueError("Day 0 must precede the first scored session")
        calendar = xcals.get_calendar("XNYS")
        for day in (self.day_zero, *self.scoring_sessions):
            if not calendar.is_session(pd.Timestamp(day)):
                raise ValueError(f"{day} is not an exchange session")
        if self.purchase_session not in (self.day_zero, *self.scoring_sessions):
            raise ValueError("Mandatory purchase session must be Day 0 or an explicit scored session")
        ids = [c.con_id for c in self.contracts]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate contract IDs")
        if not self.mandatory or any(q == 0 for q in self.mandatory.values()):
            raise ValueError("Mandatory positions must have nonzero fixed quantities")
        if self.contest_rules == "practice" and any(q < 0 for q in self.mandatory.values()):
            raise ValueError("Signed mandates require the spcx_rklb_spy contest rules")
        if self.universe == "open":
            object.__setattr__(
                self,
                "allowlist",
                tuple(
                    c.con_id
                    for c in self.contracts
                    if c.con_id not in self.mandatory and not self.prohibited_reason(c)
                ),
            )
        if not set(self.mandatory).issubset(ids) or not set(self.allowlist).issubset(ids):
            raise ValueError("Every mandate and allowlist ID must have a contract definition")
        if set(self.mandatory) & set(self.allowlist):
            raise ValueError("Mandatory contracts cannot be discretionary hedge instruments")
        if len(set(self.allowlist)) != len(self.allowlist):
            raise ValueError("Duplicate allowlist IDs")
        from .contest_rules import validate_portfolio

        mandate_symbols = {self.contract_map[i].symbol.upper() for i in self.mandatory}
        if {"SPCX", "RKLB", "SPY"}.issubset(mandate_symbols) and self.contest_rules != "spcx_rklb_spy":
            raise ValueError("SPCX/RKLB/SPY contest mandates require explicit contest rules")
        if self.contest_rules == "spcx_rklb_spy":
            validate_portfolio(self)
        for i in self.allowlist:
            if reason := self.prohibited_reason(self.contract_map[i]):
                raise ValueError(f"Forbidden allowlist conId {i}: {reason}")
        for c in self.contracts:
            if c.kind == "OPT":
                if c.expiry < self.scoring_sessions[-1]:
                    raise ValueError(f"{c.label} expires before the final holding session")
                if c.underlying_id not in ids:
                    raise ValueError(f"{c.label} needs its underlying contract")
        if not self.valuation_convention.strip() or not self.report_source.strip():
            raise ValueError("Official valuation convention and report source required")
        self._validate_burner()
        return self

    def _validate_burner(self):
        burner = self.burner
        if burner.mode != "off":
            # Exits must always fit inside the ordinary order window; RiskGate's window is never
            # relaxed. A disabled burner never constrains the ordinary policy cutoffs.
            if burner.stock_entry_cutoff_minutes < self.policy.stock_cutoff_minutes + 2:
                raise ValueError(
                    "Burner stock entry cutoff must leave 2+ minutes before the stock order cutoff"
                )
            if (
                burner.option_burn
                and burner.option_entry_cutoff_minutes < self.policy.option_cutoff_minutes + 2
            ):
                raise ValueError(
                    "Burner option entry cutoff must leave 2+ minutes before the option order cutoff"
                )
            if not burner.symbols and not burner.option_burn:
                raise ValueError("An enabled burner needs stock symbols or option burning")
            if self.policy.burn_surplus:
                # Otherwise the hedger spends surplus from the open, which the burner exists to avoid.
                raise ValueError(
                    "An enabled closing burner requires policy.burn_surplus: false "
                    "(surplus is spent only near the close)"
                )
        for symbol in burner.symbols:
            if symbol == "SPY":
                raise ValueError("SPY shares are forbidden and cannot be burner symbols")
            if self.burner_stock_id(symbol) is None:
                raise ValueError(
                    f"Burner symbol {symbol} must be a defined, allowlisted, permitted stock contract"
                )

    def burner_stock_id(self, symbol: str) -> int | None:
        """The allowlisted, permitted, non-mandated STK conId for a burner symbol (else None)."""
        return next(
            (
                c.con_id
                for c in self.contracts
                if c.kind == "STK"
                and c.symbol.upper() == symbol.upper()
                and c.con_id in self.allowlist
                and c.con_id not in self.mandatory
                and not self.prohibited_reason(c)
            ),
            None,
        )

    @property
    def contract_map(self):
        return {c.con_id: c for c in self.contracts}

    def spot_vol_rho(self, symbol: str) -> float:
        key = symbol.upper()
        default = INDEX_SPOT_VOL_CORRELATION if key in BROAD_INDEX_ETFS else SINGLE_NAME_SPOT_VOL_CORRELATION
        return self.spot_vol_correlation.get(key, default)

    def prohibited_reason(self, contract):
        from .contest_rules import prohibited_reason

        return prohibited_reason(self, contract)

    @property
    def config_hash(self):
        return digest(self)

    @property
    def contest_id(self):
        # Strategy/risk/allowlist revisions must not erase official score history.
        return digest(
            {
                "name": self.name,
                "account": self.account,
                "day_zero": self.day_zero,
                "sessions": self.scoring_sessions,
                "valuation": self.valuation_convention,
            }
        )

    def session_close(self, day: date) -> datetime:
        return xcals.get_calendar("XNYS").session_close(pd.Timestamp(day)).to_pydatetime()

    def is_session(self, day: date) -> bool:
        return xcals.get_calendar("XNYS").is_session(pd.Timestamp(day))

    def session_open(self, day: date) -> datetime:
        return xcals.get_calendar("XNYS").session_open(pd.Timestamp(day)).to_pydatetime()

    def active_session(self, now: datetime) -> date | None:
        day = now.astimezone(NY).date()
        return day if day in (self.day_zero, *self.scoring_sessions) else None

    @property
    def extended_hours(self) -> bool:
        """Contest: shares also trade in the pre-market session, and a post-market trade still books
        to the day's statement (only the closing burner trades then; see can_trade)."""
        return self.contest_rules == "spcx_rklb_spy"

    def scoring_close(self, day: date) -> datetime:
        """When the day's scored NAV stops moving with the market: the regular close. The contest
        scores IBKR's Activity Statement NAV, which marks the day's end positions at the 16:00
        regular-session closing prices (stocks) and closing midpoints (options; SPY's at 16:15):
        verified to the cent on the Oct 22 statement. Later trades change it only through their
        price versus those marks (trading_end)."""
        return self.session_close(day)

    def option_close(self, day: date, symbol: str) -> datetime:
        """When an option on `symbol` stops trading and is marked: 16:15 ET for SPY-style ETFs."""
        close = self.session_close(day)
        return close + LATE_OPTION_CLOSE if symbol in LATE_OPTION_SYMBOLS else close

    def trading_end(self, day: date) -> datetime:
        """The last moment a trade still books to the day's statement: the end of the post-market
        session (20:00 ET) in the contest, the regular close otherwise."""
        close = self.session_close(day)
        return close + EXTENDED_HOURS if self.extended_hours else close

    def post_market(self, now: datetime) -> date | None:
        """The contest session whose post-market window [close, close + 4 h) contains now."""
        day = self.active_session(now)
        if not self.extended_hours or day is None:
            return None
        close = self.session_close(day)
        return day if close <= now < close + EXTENDED_HOURS else None

    def pre_market(self, now: datetime) -> date | None:
        """The contest session whose pre-market window [open - 5 h 30, open) contains now."""
        day = self.active_session(now)
        if not self.extended_hours or day is None:
            return None
        opened = self.session_open(day)
        return day if opened - PRE_MARKET <= now < opened else None

    def extended_session(self, now: datetime) -> str | None:
        """ "pre" or "post" while a contest session's shares trade outside regular hours."""
        if self.pre_market(now) is not None:
            return "pre"
        return "post" if self.post_market(now) is not None else None

    def next_close(self, now: datetime) -> datetime | None:
        close = next(
            (self.scoring_close(d) for d in self.scoring_sessions if self.scoring_close(d) > now), None
        )
        if close is None and self.scoring_sessions and now < self.trading_end(self.scoring_sessions[-1]):
            # After the final close the post-market session still books to the last statement.
            return self.trading_end(self.scoring_sessions[-1])
        return close

    def can_trade(self, now: datetime, kind: str, setup: bool = False, post_market: bool = False) -> bool:
        day = self.active_session(now)
        if day is None:
            return False
        cutoff = (
            self.execution.setup_cutoff_minutes
            if setup
            else self.policy.option_cutoff_minutes
            if kind == "OPT"
            else self.policy.stock_cutoff_minutes
        )
        if self.session_open(day) <= now < self.session_close(day) - timedelta(minutes=cutoff):
            return True
        # Contest pre-market session (until the open): shares only. The post-market session (until
        # the same cutoff before 20:00 ET) only for post_market callers, the closing burner: the
        # statement already marks every position at the close, so a later trade only books its
        # price versus that mark plus fees, never a hedge.
        if kind != "STK" or setup:
            return False
        if self.pre_market(now) is not None:
            return True
        return (
            post_market
            and self.post_market(now) is not None
            and now < self.trading_end(day) - timedelta(minutes=cutoff)
        )

    def accrual_days(self, day: date) -> int:
        days = (self.day_zero, *self.scoring_sessions)
        if day not in self.scoring_sessions:
            raise ValueError(f"{day} is not a scored session")
        return (day - days[days.index(day) - 1]).days

    def next_scored_session(self, day: date) -> date | None:
        return next((d for d in self.scoring_sessions if d > day), None)

    def benchmark_growth(self, day: date) -> float:
        """Modeled target growth over the previous close for scored ``day``; 0 when unknown."""
        rate = self.scoring_daily_rates.get(day, self.scoring_forecast_daily_rate)
        try:
            return float(self.scoring_target(Decimal(1), day, rate) - 1)
        except ValueError:
            return 0.0

    @property
    def purchase_session(self) -> date:
        return self.mandatory_purchase_session or self.day_zero

    def scoring_target(
        self, previous_nav: Decimal, day: date | None = None, daily_rate: Decimal | None = None
    ) -> Decimal:
        if self.scoring_rule == "pending_interest_adjustment":
            raise ValueError("Scoring formula pending")
        if self.scoring_rule == "interest_benchmark":
            return previous_nav * (Decimal(1) + self.scoring_annual_rate / Decimal(360))
        if self.scoring_rule == "calendar_interest_benchmark":
            # (1 + r/360)^n_i, where n_i counts calendar days since the previous
            # contest session (3 on a Monday after a normal weekend).
            factor = Decimal(1) + self.scoring_annual_rate / Decimal(360)
            return previous_nav * factor ** self.accrual_days(day)
        if self.scoring_rule == "actual_daily_cash_rate":
            rate = daily_rate if daily_rate is not None else self.scoring_daily_rates.get(day)
            if rate is None:
                raise ValueError("Actual daily cash benchmark rate missing")
            return previous_nav * (Decimal(1) + rate)
        return previous_nav


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.model_validate(yaml.safe_load(Path(path).read_text()))

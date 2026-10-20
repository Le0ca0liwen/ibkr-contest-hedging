import csv
from datetime import date
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import select

from .config import Manifest
from .models import utcnow
from .storage import ClosingNAV, Record, Store

# Closing-NAV provenance is derived from the stored source text, so no schema migration is needed.
# Recorder captures are provisional; any other source (dashboard entry, CSV statement import) is
# the official IBKR statement the instructor scores.
API_SOURCES = ("IBKR API", "SYNTHETIC simulator")


def provenance(source: str) -> str:
    return "api" if source.startswith(API_SOURCES) else "statement"


class ScoreLedger:
    def __init__(self, store: Store, manifest: Manifest):
        self.store, self.manifest = store, manifest

    def import_close(self, day: date, nav: Decimal, source: str, account: str):
        if account != self.manifest.account:
            raise ValueError("Report account does not match contest")
        if day not in (self.manifest.day_zero, *self.manifest.scoring_sessions):
            raise ValueError("Report day is not in the contest")
        if not nav.is_finite() or not source.strip():
            raise ValueError("Finite official NAV and report provenance required")
        with self.store.transaction() as session:
            session.add(
                ClosingNAV(
                    account=account,
                    contest=self.manifest.contest_id,
                    day=day,
                    nav=nav,
                    source=source,
                    at=utcnow(),
                )
            )
        return self.summary()

    def import_csv(self, path):
        # Validate the whole file before mutation; no partial imports.
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        validated = []
        for row in rows:
            day, nav = date.fromisoformat(row["date"]), Decimal(row["nav"])
            if row["account"] != self.manifest.account or day not in (
                self.manifest.day_zero,
                *self.manifest.scoring_sessions,
            ):
                raise ValueError("Invalid report account/session")
            if not nav.is_finite() or not row["source"].strip():
                raise ValueError("Invalid NAV/provenance")
            validated.append((day, nav, row["source"], row["account"]))
        with self.store.transaction() as session:
            for day, nav, source, account in validated:
                session.add(
                    ClosingNAV(
                        account=account,
                        contest=self.manifest.contest_id,
                        day=day,
                        nav=nav,
                        source=source,
                        at=utcnow(),
                    )
                )
        return self.summary()

    def revisions(self):
        """Every closing-NAV row in insertion order, with derived provenance."""
        with self.store.sessions() as session:
            rows = session.scalars(
                select(ClosingNAV)
                .where(
                    ClosingNAV.account == self.manifest.account,
                    ClosingNAV.contest == self.manifest.contest_id,
                )
                .order_by(ClosingNAV.id)
            ).all()
            return [
                {"day": r.day, "nav": r.nav, "source": r.source, "provenance": provenance(r.source)}
                for r in rows
            ]

    def close_records(self, revisions=None):
        """Effective close per day: the latest statement row beats any API row regardless of
        insertion order; among API rows (bell and post-close captures) the latest wins."""
        chosen = {}
        for row in self.revisions() if revisions is None else revisions:
            current = chosen.get(row["day"])
            if current is None or row["provenance"] == "statement" or current["provenance"] == "api":
                chosen[row["day"]] = row
        return chosen

    def closes(self):
        return {day: row["nav"] for day, row in self.close_records().items()}

    def previous_close_record(self, day):
        days = (self.manifest.day_zero, *self.manifest.scoring_sessions)
        if day not in days or day == days[0]:
            return None
        return self.close_records().get(days[days.index(day) - 1])

    def previous_close(self, day):
        record = self.previous_close_record(day)
        return record["nav"] if record else None

    def latest_report(self):
        records = self.close_records()
        if not records:
            return None
        day = max(records)
        row = records[day]
        return {
            "date": str(day),
            "nav": str(row["nav"]),
            "source": row["source"],
            "provenance": row["provenance"],
        }

    def daily_rates(self):
        rates = dict(self.manifest.scoring_daily_rates)
        with self.store.sessions() as session:
            rows = session.scalars(
                select(Record)
                .where(Record.category == "daily_cash_benchmark", Record.account == self.manifest.account)
                .order_by(Record.at, Record.id)
            ).all()
            for row in rows:
                if row.payload.get("contest") == self.manifest.contest_id:
                    rates[date.fromisoformat(row.payload["date"])] = Decimal(row.payload["daily_rate"])
        return rates

    def import_daily_rate(self, day, daily_rate, source):
        if (
            day not in self.manifest.scoring_sessions
            or not daily_rate.is_finite()
            or not Decimal(0) <= daily_rate <= Decimal(1)
            or not source.strip()
        ):
            raise ValueError("Scored date, finite fractional daily cash rate, and provenance required")
        self.store.record(
            "daily_cash_benchmark",
            self.manifest.account,
            {
                "contest": self.manifest.contest_id,
                "date": str(day),
                "daily_rate": str(daily_rate),
                "source": source,
            },
        )
        return self.summary()

    def target_for(self, day, previous=None, allow_estimate=False):
        previous = previous if previous is not None else self.previous_close(day)
        if previous is None:
            return None
        try:
            rate = self.daily_rates().get(day)
            if rate is None and allow_estimate:
                rate = self.manifest.scoring_forecast_daily_rate
            return self.manifest.scoring_target(previous, day, rate)
        except ValueError:
            return None

    def summary(self):
        pending = self.manifest.scoring_rule == "pending_interest_adjustment"
        days = (self.manifest.day_zero, *self.manifest.scoring_sessions)
        records = self.close_records()
        closes = {day: row["nav"] for day, row in records.items()}
        result, total = [], Decimal(0)
        for previous, day in pairwise(days):
            target = self.target_for(day, closes.get(previous)) if previous in closes else None
            score = (
                abs(closes[day] - target) if not pending and day in closes and target is not None else None
            )
            if score is not None:
                total += score
            result.append(
                {
                    "date": str(day),
                    "nav": str(closes[day]) if day in closes else None,
                    "provenance": records[day]["provenance"] if day in records else None,
                    "previous_date": str(previous),
                    "target_nav": str(target) if target is not None else None,
                    "daily_cash_rate": str(self.daily_rates()[day]) if day in self.daily_rates() else None,
                    "score": str(score) if score is not None else None,
                }
            )
        return {
            "day_zero": str(days[0]),
            "baseline": str(closes[days[0]]) if days[0] in closes else None,
            "baseline_provenance": records[days[0]]["provenance"] if days[0] in records else None,
            "days": result,
            "total": None
            if pending
            or (
                self.manifest.scoring_rule == "actual_daily_cash_rate"
                and any(r["score"] is None for r in result)
            )
            else str(total),
            "scoring_rule": self.manifest.scoring_rule,
            "annual_rate": str(self.manifest.scoring_annual_rate)
            if self.manifest.scoring_annual_rate is not None
            else None,
            "status": "Awaiting instructor's interest-adjusted formula"
            if pending
            else "Actual daily cash rate · missing rates or closes remain unscored"
            if self.manifest.scoring_rule == "actual_daily_cash_rate"
            else "Interest benchmark: previous NAV × (1 + r/360)"
            if self.manifest.scoring_rule == "interest_benchmark"
            else "Calendar interest benchmark: previous NAV × (1 + r/360)^n"
            if self.manifest.scoring_rule == "calendar_interest_benchmark"
            else "Raw NAV absolute changes",
            "complete": all(r["score"] is not None for r in result),
        }

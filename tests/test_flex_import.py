"""The nightly IBKR Activity Statement import records the statement NAV as the official close."""

from datetime import datetime, timedelta
from decimal import Decimal

from hedge.config import NY
from hedge.flex import FlexError
from hedge.flex_import import StatementImporter
from hedge.service import TradingService


def report(account, day, total="983374.21"):
    d = f"{day:%Y%m%d}"
    return f"""<FlexQueryResponse queryName="Contest NAV" type="AF"><FlexStatements count="1">
<FlexStatement accountId="{account}" fromDate="{d}" toDate="{d}" period="LastBusinessDay">
<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="{account}" currency="USD" reportDate="{d}"
 cash="872744.5" stock="74591.65" options="34437" interestAccruals="1601.06" total="{total}" />
</EquitySummaryInBase>
<ChangeInNAV accountId="{account}" fromDate="{d}" toDate="{d}" startingValue="981250.37" endingValue="{total}" />
<OpenPositions><OpenPosition accountId="{account}" assetCategory="OPT" symbol="X" conid="101" multiplier="100"
 strike="200" expiry="20261120" putCall="C" reportDate="{d}" position="-5" markPrice="6.55" positionValue="-3275"
 levelOfDetail="SUMMARY" /></OpenPositions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute)


def importer(manifest, store, answers):
    service = TradingService(manifest, store, demo=True)
    calls = []

    async def fetch(token, query, *, from_date=None, to_date=None):
        calls.append((token, query, from_date, to_date))
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(from_date) if callable(answer) else answer

    imp = StatementImporter(service, fetch=fetch, settings=lambda root: ("1234567890", "42"))
    return service, imp, calls


async def test_after_midnight_the_statement_nav_becomes_the_official_close(manifest, store):
    day = manifest.scoring_sessions[0]
    totals = {manifest.day_zero: "981250.37", day: "983374.21"}
    service, imp, calls = importer(manifest, store, [lambda d: report(manifest.account, d, totals[d])])
    service.ledger.import_close(manifest.day_zero, Decimal("981250.37"), "IBKR statement", manifest.account)
    service.ledger.import_close(
        day, Decimal("983514.38"), "IBKR API end-of-day observation", manifest.account
    )
    next_day = day + timedelta(days=1)
    assert imp.due(at(next_day, 0, 9)) == [
        manifest.day_zero
    ]  # day 0 is backfilled; statements appear ~midnight
    await imp.step(at(next_day, 0, 9))
    payload = await imp.step(at(next_day, 0, 10))
    assert [c[2:] for c in calls] == [(manifest.day_zero, manifest.day_zero), (day, day)]  # one day each
    assert calls[1][:2] == ("1234567890", "42")
    record = service.ledger.close_records()[day]
    assert record["nav"] == Decimal("983374.21") and record["provenance"] == "statement"
    assert record["source"].startswith(f"IBKR Flex Activity Statement {manifest.account} NAV for {day}")
    assert payload["positions"]["101"]["mark"] == "6.55" and payload["provisional_before"] == "983514.38"
    alerts = [a["message"] for a in store.records("alert", manifest.account)]
    assert any("provisional close was 983,514.38 (-140.17)" in a for a in alerts)
    assert imp.state()["latest"]["total"] == "983374.21"
    assert await imp.step(at(next_day, 0, 40)) is None and len(calls) == 2  # done for both days


async def test_day_0_is_backfilled_and_an_equal_entered_statement_is_not_duplicated(manifest, store):
    service, imp, calls = importer(manifest, store, [lambda d: report(manifest.account, d, "1000000.00")])
    service.ledger.import_close(manifest.day_zero, Decimal("1000000.00"), "IBKR statement", manifest.account)
    now = at(manifest.day_zero + timedelta(days=1), 3)
    assert imp.due(now) == [manifest.day_zero]
    await imp.step(now)
    assert len(service.ledger.revisions()) == 1  # same value: recorded as checked, not re-imported
    assert "matches the recorded close" in imp.status


async def test_a_different_entered_close_is_replaced_and_flagged(manifest, store):
    day = manifest.day_zero
    service, imp, _ = importer(manifest, store, [lambda d: report(manifest.account, d)])
    service.ledger.import_close(day, Decimal("983347.21"), "operator entry", manifest.account)  # a typo
    await imp.step(at(day + timedelta(days=1), 1))
    assert service.ledger.closes()[day] == Decimal("983374.21")
    assert any(
        "differs from the entered close" in a["message"] for a in store.records("alert", manifest.account)
    )


async def test_not_ready_is_retried_every_30_minutes_and_flagged_after_two_hours(manifest, store):
    day = manifest.day_zero
    not_ready = FlexError("IBKR Flex error 1019: generation in progress", code=1019, retry=True)
    service, imp, calls = importer(manifest, store, [not_ready])
    start = at(day + timedelta(days=1), 0, 10)
    for minutes in (0, 10, 30, 60, 90):
        await imp.step(start + timedelta(minutes=minutes))
    assert len(calls) == 4  # 00:10, 00:40, 01:10, 01:40
    assert any("import for" in a["message"] for a in store.records("alert", manifest.account))
    assert "not imported yet" in imp.status and imp.state()["next_try"]
    assert day not in service.ledger.closes()


async def test_a_wrong_account_or_bad_token_is_alerted_at_once(manifest, store):
    day = manifest.day_zero
    service, imp, calls = importer(manifest, store, [lambda d: report("DU0000000", d)])
    now = at(day + timedelta(days=1), 0, 10)
    await imp.step(now)
    alerts = [a["message"] for a in store.records("alert", manifest.account)]
    assert any("reports account DU0000000" in a and "Setup IBKR Flex" in a for a in alerts)
    await imp.step(now + timedelta(hours=1))
    assert len(calls) == 1  # a fatal problem waits for the operator, not every 30 minutes
    assert day not in service.ledger.closes()


async def test_nothing_runs_without_the_operators_flex_settings(manifest, store):
    service = TradingService(manifest, store, demo=True)
    imp = StatementImporter(service, settings=lambda root: None)
    assert await imp.step(at(manifest.day_zero + timedelta(days=1), 1)) is None
    assert not imp.state()["configured"] and "Setup IBKR Flex.command" in imp.status


async def test_the_statements_extra_interest_accrual_is_learned(manifest, store):
    """IBKR's AccruedCash lags the statement: the gap at one statement is added to later predictions
    (Fridays learn from Fridays, since a Friday statement may carry the weekend)."""
    day = manifest.day_zero
    service, imp, _ = importer(manifest, store, [lambda d: report(manifest.account, d)])
    store.record("close_marks", manifest.account, {"date": str(day), "at": "x", "accrued_cash": "1520.00"})
    await imp.step(at(day + timedelta(days=1), 0, 10))
    assert store.records("flex_statement", manifest.account)[0]["accrual_gap"] == "81.06"
    later = manifest.scoring_sessions[0]
    interest = service._interest_adjustment(later)
    assert interest["amount"] == 81.06 and str(day) in interest["source"]
    assert service._interest_adjustment(day)["amount"] == 0.0  # never from its own or a later statement

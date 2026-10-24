import json
from datetime import date

from hedge.flex import FlexError
from ops.statement_history import backfill, business_days


def report(day):
    d = f"{day:%Y%m%d}"
    return f"""<FlexQueryResponse><FlexStatements count="1"><FlexStatement accountId="DU1" fromDate="{d}" toDate="{d}">
<EquitySummaryInBase><EquitySummaryByReportDateInBase reportDate="{d}" total="1000" /></EquitySummaryInBase>
<OpenPositions><OpenPosition assetCategory="OPT" symbol="X" conid="7" multiplier="100" strike="5" expiry="20261120"
 putCall="P" reportDate="{d}" position="2" markPrice="1.5" positionValue="300" levelOfDetail="SUMMARY" /></OpenPositions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def recording(answers):
    calls, sleeps = [], []

    async def fetch(token, query, from_date=None, to_date=None, attempts=None):
        calls.append((from_date, attempts))
        answer = answers.get(from_date, "report")
        if isinstance(answer, Exception):
            raise answer
        return report(from_date) if answer == "report" else answer

    async def sleep(seconds):
        sleeps.append(seconds)

    return calls, sleeps, fetch, sleep


def unavailable():
    return FlexError("IBKR Flex error 1003: Statement is not available.", code=1003, retry=True)


async def test_newest_first_each_day_once_and_a_lone_missing_day_is_skipped(tmp_path):
    days = business_days(date(2026, 10, 23), 4)
    assert days == [date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21), date(2026, 10, 20)]
    calls, sleeps, fetch, sleep = recording({date(2026, 10, 22): unavailable()})  # e.g. a holiday
    done, failed = await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert done == [date(2026, 10, 23), date(2026, 10, 21), date(2026, 10, 20)]
    assert [d for d, _ in failed] == [date(2026, 10, 22)] and sleeps == [15, 15, 15]
    assert all(attempts == 3 for _, attempts in calls)  # fewer polls than the app's import
    data = json.loads((tmp_path / "2026-10-21.json").read_text())
    assert data["positions"][0]["mark"] == "1.5"
    calls.clear()
    await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert [d for d, _ in calls] == [date(2026, 10, 22)]  # on disk: never asked again


async def test_two_missing_days_in_a_row_end_the_history(tmp_path):
    """Oct 23: polling 1003 for every day before the paper account existed locked the token."""
    days = business_days(date(2026, 10, 23), 10)
    gone = {d: unavailable() for d in days if d < date(2026, 10, 19)}
    calls, _, fetch, sleep = recording(gone)
    done, _ = await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert len(done) == 5 and len(calls) == 7  # Oct 19-23, then two misses and stop


async def test_a_locked_token_or_network_failure_stops_the_run_at_once(tmp_path):
    days = business_days(date(2026, 10, 23), 5)
    locked = FlexError("IBKR Flex error 1025: Too many failed attempts.", code=1025, retry=True)
    calls, _, fetch, sleep = recording({date(2026, 10, 22): locked})
    done, failed = await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert (
        done == [date(2026, 10, 23)] and len(calls) == 2 and failed[0][1].startswith("IBKR Flex error 1025")
    )
    offline = FlexError("IBKR Flex service unreachable (ConnectError)", retry=True)
    calls, _, fetch, sleep = recording({date(2026, 10, 22): offline})
    await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert [d for d, _ in calls] == [date(2026, 10, 22)]

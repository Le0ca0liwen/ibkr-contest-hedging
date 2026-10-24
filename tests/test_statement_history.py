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


async def test_every_business_day_is_written_once_and_failures_are_kept(tmp_path):
    days = business_days(date(2026, 10, 23), 3)
    assert days == [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]
    calls, sleeps = [], []

    async def fetch(token, query, from_date=None, to_date=None):
        calls.append(from_date)
        if from_date == date(2026, 10, 22):
            raise FlexError("IBKR Flex error 1003: Statement is not available", code=1003, retry=True)
        return report(from_date)

    async def sleep(seconds):
        sleeps.append(seconds)

    done, failed = await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert done == [date(2026, 10, 21), date(2026, 10, 23)] and failed[0][0] == date(2026, 10, 22)
    data = json.loads((tmp_path / "2026-10-21.json").read_text())
    assert data["positions"][0]["mark"] == "1.5" and sleeps == [15, 15]
    await backfill("t", "q", days, tmp_path, fetch, sleep)
    assert calls.count(date(2026, 10, 21)) == 1  # on disk: never asked again

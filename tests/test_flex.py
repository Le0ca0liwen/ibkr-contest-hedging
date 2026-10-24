import io
import json
import logging
import os
import stat
from datetime import date
from decimal import Decimal

import httpx
import pytest

from hedge.flex import (
    GET_URL,
    SEND_URL,
    USER_AGENT,
    FlexError,
    fetch_statement,
    parse_statement,
    read_flex_settings,
)

TOKEN = "123456789012345678901234"
QUERY = "987654"
REFERENCE = "5551234567"

REPORT = """<FlexQueryResponse queryName="Contest NAV" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="DU1234567" fromDate="20261022" toDate="20261022" period="LastBusinessDay"
    whenGenerated="20261023;051214">
<EquitySummaryInBase>
<EquitySummaryByReportDateInBase accountId="DU1234567" acctAlias="" model="" currency="USD"
    reportDate="20261021" cash="870120.15" stock="75012.4" options="34518.75" interestAccruals="1599.07"
    total="981250.37" totalLong="987400.12" totalShort="-6149.75" />
<EquitySummaryByReportDateInBase accountId="DU1234567" acctAlias="" model="" currency="USD"
    reportDate="20261022" cash="872744.5" stock="74591.65" options="34437" interestAccruals="1601.06"
    total="983374.21" totalLong="989512.96" totalShort="-6138.75" />
</EquitySummaryInBase>
<ChangeInNAV accountId="DU1234567" acctAlias="" model="" currency="USD" fromDate="20261022"
    toDate="20261022" startingValue="981250.37" mtm="2210.43" realized="0" dividends="0"
    interest="1.99" commissions="-88.58" endingValue="983374.21" />
<OpenPositions>
<OpenPosition accountId="DU1234567" acctAlias="" model="" currency="USD" fxRateToBase="1"
    assetCategory="STK" symbol="RKLB" description="ROCKET LAB CORP" conid="473476196" securityID=""
    multiplier="1" strike="" expiry="" putCall="" reportDate="20261022" position="1120"
    markPrice="51.22" positionValue="57366.4" side="Long" levelOfDetail="SUMMARY" />
<OpenPosition accountId="DU1234567" acctAlias="" model="" currency="USD" fxRateToBase="1"
    assetCategory="OPT" symbol="RKLB  261120P00055000" description="RKLB 20NOV26 55 P" conid="812345678"
    underlyingSymbol="RKLB" multiplier="100" strike="55" expiry="20261120" putCall="P"
    reportDate="20261022" position="25" markPrice="6.55" positionValue="16375" side="Long"
    levelOfDetail="SUMMARY" />
<OpenPosition accountId="DU1234567" assetCategory="OPT" symbol="RKLB  261120P00055000"
    conid="812345678" multiplier="100" strike="55" expiry="20261120" putCall="P" reportDate="20261022"
    position="25" markPrice="6.55" positionValue="16375" levelOfDetail="LOT" />
</OpenPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>
"""


def status_xml(status, code=None, message=None, reference=None, url=None):
    parts = [f"<Status>{status}</Status>"]
    if reference:
        parts.append(f"<ReferenceCode>{reference}</ReferenceCode>")
    if url:
        parts.append(f"<Url>{url}</Url>")
    if code:
        parts.append(f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{message}</ErrorMessage>")
    return f'<FlexStatementResponse timestamp="23 October, 2026 05:12 AM EDT">{"".join(parts)}</FlexStatementResponse>'


SENT = status_xml("Success", reference=REFERENCE, url=GET_URL)
IN_PROGRESS = status_xml("Warn", 1019, "Statement generation in progress. Please try again shortly.")


def scripted(*bodies, status=200):
    """A MockTransport answering each request with the next body; records every request."""
    requests, queue = [], list(bodies)

    def handler(request):
        requests.append(request)
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), requests


def recorder():
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    return sleeps, sleep


def assert_secret(error):
    for text in (str(error), repr(error), *map(str, error.args)):
        assert TOKEN not in text


async def test_fetch_polls_while_the_statement_is_generated_and_sends_a_user_agent():
    transport, requests = scripted(SENT, IN_PROGRESS, REPORT)
    sleeps, sleep = recorder()
    text = await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert text == REPORT and sleeps == [5, 5]
    assert [str(r.url.copy_with(query=None)) for r in requests] == [SEND_URL, GET_URL, GET_URL]
    assert all(r.headers["User-Agent"] == USER_AGENT for r in requests)
    assert [dict(r.url.params) for r in requests] == [
        {"t": TOKEN, "q": QUERY, "v": "3"},
        {"t": TOKEN, "q": REFERENCE, "v": "3"},
        {"t": TOKEN, "q": REFERENCE, "v": "3"},
    ]
    assert parse_statement(text)["ending_value"] == Decimal("983374.21")


async def test_a_date_override_asks_for_that_day_only():
    transport, requests = scripted(SENT, REPORT)
    sleeps, sleep = recorder()
    day = date(2026, 10, 22)
    await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep, from_date=day, to_date=day)
    assert dict(requests[0].url.params) == {
        "t": TOKEN,
        "q": QUERY,
        "v": "3",
        "fd": "20261022",
        "td": "20261022",
    }
    assert dict(requests[1].url.params) == {"t": TOKEN, "q": REFERENCE, "v": "3"}
    with pytest.raises(FlexError, match="both"):
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep, from_date=day)


async def test_a_fatal_code_raises_without_the_token_and_stops():
    expired = status_xml("Fail", 1012, f"Token {TOKEN} has expired.")
    transport, requests = scripted(expired)
    sleeps, sleep = recorder()
    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert caught.value.code == 1012 and caught.value.retry is False
    assert "1012" in str(caught.value) and "has expired" in str(caught.value)
    assert_secret(caught.value)
    assert len(requests) == 1 and sleeps == []


async def test_a_fatal_code_from_get_statement_is_not_retried():
    transport, requests = scripted(SENT, status_xml("Fail", 1015, "Token is invalid."))
    _, sleep = recorder()
    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert (caught.value.code, caught.value.retry, len(requests)) == (1015, False, 2)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (1003, "Statement is not available."),
        (1025, "Too many failed attempts. Please review your configuration."),
    ],
)
async def test_not_available_and_locked_out_are_answered_once_never_polled(code, message):
    """Oct 23: polling 1003 six times a day for days before the account existed made IBKR lock
    the token (1025). Both are worth asking again only much later, so each costs one request."""
    transport, requests = scripted(SENT, status_xml("Fail", code, message))
    sleeps, sleep = recorder()
    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert (caught.value.code, caught.value.retry, len(requests)) == (code, True, 2)
    assert sleeps == [5] and message in str(caught.value)
    assert_secret(caught.value)


async def test_still_in_progress_after_every_attempt_is_retryable():
    transport, requests = scripted(SENT, IN_PROGRESS)
    sleeps, sleep = recorder()
    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep, attempts=3, wait_seconds=0)
    assert (caught.value.code, caught.value.retry) == (1019, True)
    assert len(requests) == 4 and sleeps == [1.0, 1.0, 1.0]  # never faster than one request a second
    assert_secret(caught.value)


async def test_a_rate_limited_send_request_is_asked_again():
    limited = status_xml("Fail", 1018, "Too many requests have been made from this token.")
    transport, requests = scripted(limited, SENT, REPORT)
    sleeps, sleep = recorder()
    assert await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep) == REPORT
    assert [r.url.params["q"] for r in requests] == [QUERY, QUERY, REFERENCE]
    assert sleeps == [60, 5]  # a rate limit waits out the ten-a-minute window


async def test_more_than_ten_requests_wait_for_the_per_minute_limit():
    transport, _ = scripted(SENT, IN_PROGRESS, REPORT)
    sleeps, sleep = recorder()
    await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep, attempts=12, wait_seconds=2)
    assert sleeps == [6.7, 6.7]


async def test_the_token_never_leaves_interactive_brokers():
    transport, requests = scripted(
        status_xml("Success", reference=REFERENCE, url="https://example.com/GetStatement"), REPORT
    )
    _, sleep = recorder()
    await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert {r.url.host for r in requests} == {"ndcdyn.interactivebrokers.com"}

    other = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
    transport, requests = scripted(status_xml("Success", reference=REFERENCE, url=other), REPORT)
    await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert str(requests[1].url.copy_with(query=None)) == other


async def test_http_and_network_failures_never_carry_the_token():
    _, sleep = recorder()
    for status, retry in ((503, True), (429, True), (302, True), (403, False)):
        transport, requests = scripted("Service Unavailable", status=status)
        with pytest.raises(FlexError) as caught:
            await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
        assert (caught.value.retry, caught.value.code, len(requests)) == (retry, None, 1)
        assert_secret(caught.value)

    def unreachable(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=httpx.MockTransport(unreachable), sleep=sleep)
    assert caught.value.retry is True and caught.value.__cause__ is None
    assert caught.value.__context__ is None  # the httpx error's request URL holds the token
    assert_secret(caught.value)


async def test_csv_and_doctype_reports_are_refused():
    _, sleep = recorder()
    transport, _ = scripted(SENT, '"ClientAccountID","Total"\n"DU1234567","983374.21"\n')
    with pytest.raises(FlexError, match="Format to XML") as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert caught.value.retry is False

    transport, _ = scripted("Service temporarily unavailable")  # not a report: no Format advice
    with pytest.raises(FlexError) as caught:
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert caught.value.retry is True and "Format" not in str(caught.value)

    hostile = '<!DOCTYPE x [<!ENTITY big "aaaa">]><FlexQueryResponse>&big;</FlexQueryResponse>'
    transport, _ = scripted(SENT, hostile)
    with pytest.raises(FlexError, match="unsafe"):
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)


async def test_a_report_with_a_byte_order_mark_is_accepted():
    transport, _ = scripted(SENT, "\ufeff" + REPORT)
    _, sleep = recorder()
    text = await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert parse_statement(text)["nav"] == parse_statement(REPORT)["nav"]


async def test_the_httpx_request_log_masks_the_token(caplog):
    transport, _ = scripted(SENT, REPORT)
    _, sleep = recorder()
    with caplog.at_level(logging.INFO, logger="httpx"):
        await fetch_statement(TOKEN, QUERY, transport=transport, sleep=sleep)
    assert "FlexWebService" in caplog.text and "t=***" in caplog.text
    assert TOKEN not in caplog.text


def test_parse_statement_reads_nav_change_and_positions():
    parsed = parse_statement(REPORT)
    assert parsed["account"] == "DU1234567"
    assert parsed["from"] == parsed["to"] == parsed["ending_date"] == date(2026, 10, 22)
    assert set(parsed["nav"]) == {date(2026, 10, 21), date(2026, 10, 22)}
    day = parsed["nav"][date(2026, 10, 22)]
    assert day == {
        "total": Decimal("983374.21"),
        "cash": Decimal("872744.50"),
        "stock": Decimal("74591.65"),
        "options": Decimal("34437.00"),
        "interest_accruals": Decimal("1601.06"),
    }
    assert day["cash"] + day["stock"] + day["options"] + day["interest_accruals"] == day["total"]
    assert parsed["ending_value"] == Decimal("983374.21")
    assert parsed["starting_value"] == parsed["nav"][date(2026, 10, 21)]["total"]
    stock, option = parsed["positions"]  # the LOT row repeats the summary and is skipped
    assert stock == {
        "conid": 473476196,
        "symbol": "RKLB",
        "asset": "STK",
        "put_call": None,
        "strike": None,
        "expiry": None,
        "multiplier": 1,
        "quantity": Decimal("1120"),
        "mark": Decimal("51.22"),
        "value": Decimal("57366.4"),
        "report_date": date(2026, 10, 22),
    }
    assert option["asset"] == "OPT" and option["put_call"] == "P" and option["strike"] == 55.0
    assert option["expiry"] == date(2026, 11, 20) and option["multiplier"] == 100
    assert (option["quantity"], option["mark"], option["value"]) == (
        Decimal("25"),
        Decimal("6.55"),
        Decimal("16375"),
    )
    assert option["quantity"] * option["multiplier"] * option["mark"] == option["value"]


def test_parse_statement_reads_dashed_dates_and_missing_attributes():
    dashed = REPORT
    for compact, iso in (("20261021", "2026-10-21"), ("20261022", "2026-10-22"), ("20261120", "2026-11-20")):
        dashed = dashed.replace(f'"{compact}', f'"{iso}')
    assert parse_statement(dashed) == parse_statement(REPORT)

    sparse = """<FlexQueryResponse><FlexStatements count="1"><FlexStatement accountId="DU1234567">
    <EquitySummaryInBase><EquitySummaryByReportDateInBase reportDate="20261022" total="983374.21" />
    <EquitySummaryByReportDateInBase total="1" /></EquitySummaryInBase>
    <OpenPositions><OpenPosition conid="9" assetCategory="STK" position="--" />
    <OpenPosition symbol="NOCONID" /></OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>"""
    parsed = parse_statement(sparse)
    assert parsed["from"] is None and parsed["to"] is None
    assert parsed["ending_value"] is None and parsed["ending_date"] is None
    assert parsed["nav"] == {
        date(2026, 10, 22): {
            "total": Decimal("983374.21"),
            "cash": None,
            "stock": None,
            "options": None,
            "interest_accruals": None,
        }
    }
    [position] = parsed["positions"]
    assert position["conid"] == 9 and position["multiplier"] == 1
    assert position["quantity"] is None and position["mark"] is None and position["expiry"] is None


def test_parse_statement_refuses_doctype_entities_and_non_statements():
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_statement('<!DOCTYPE lolz [<!ENTITY lol "lol">]><FlexQueryResponse>&lol;</FlexQueryResponse>')
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_statement('<?xml version="1.0"?><!doctype x SYSTEM "file:///etc/passwd"><x/>')
    with pytest.raises(ValueError, match="No FlexStatement"):
        parse_statement(IN_PROGRESS)
    with pytest.raises(ValueError, match="Unreadable"):
        parse_statement("<FlexQueryResponse>")


def test_parse_statement_refuses_another_date_format_instead_of_dropping_every_row():
    american = REPORT
    for compact, us in (("20261021", "10/21/2026"), ("20261022", "10/22/2026"), ("20261120", "11/20/2026")):
        american = american.replace(f'"{compact}"', f'"{us}"')
    assert '20261022"' not in american
    with pytest.raises(ValueError, match="Date Format to yyyyMMdd"):
        parse_statement(american)


def test_setup_script_stores_the_token_and_query_privately(tmp_path, monkeypatch, capsys):
    from ops.setup_ibkr_flex import main

    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    main(["--token-stdin", "--query-id", QUERY, "--root", str(tmp_path)])
    path = tmp_path / ".runtime/ibkr-flex.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"token": TOKEN, "query_id": QUERY}
    assert read_flex_settings(tmp_path) == (TOKEN, QUERY)
    assert TOKEN not in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(f"{TOKEN}\n {QUERY} \n"))  # query ID on the next line
    main(["--token-stdin", "--root", str(tmp_path)])
    assert read_flex_settings(tmp_path) == (TOKEN, QUERY)

    for token, query in (("12345", QUERY), ("1234567890abc", QUERY), (TOKEN, "Q1")):
        monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
        with pytest.raises(ValueError) as caught:
            main(["--token-stdin", "--query-id", query, "--root", str(tmp_path)])
        assert token not in str(caught.value)


def test_read_flex_settings_requires_a_private_regular_file(tmp_path):
    folder = tmp_path / ".runtime"
    folder.mkdir()
    path = folder / "ibkr-flex.json"
    assert read_flex_settings(tmp_path) is None
    path.write_text(json.dumps({"token": TOKEN, "query_id": QUERY}))
    path.chmod(0o644)
    assert read_flex_settings(tmp_path) is None
    path.chmod(0o600)
    assert read_flex_settings(str(tmp_path)) == (TOKEN, QUERY)

    target = tmp_path / "elsewhere.json"
    path.rename(target)
    path.symlink_to(target)
    assert read_flex_settings(tmp_path) is None

    path.unlink()
    for settings in ({"token": "12345", "query_id": QUERY}, {"token": TOKEN}, ["not", "a", "dict"]):
        path.write_text(json.dumps(settings))
        path.chmod(0o600)
        assert read_flex_settings(tmp_path) is None
    path.write_text("{not json")
    assert read_flex_settings(tmp_path) is None

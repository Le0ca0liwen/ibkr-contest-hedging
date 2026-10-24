"""IBKR Flex Web Service: fetch and parse the daily Activity Statement, the contest's scored NAV.

Version 3 takes two GETs: SendRequest runs the Flex Query and returns a reference code, then
GetStatement returns the report, or a retry code (1019) while IBKR is still generating it. IBKR
allows one request per second and ten per minute per token, and every request needs a User-Agent.
The token travels in the query string: it never goes into an exception message, and every URL this
module renders, httpx's request log included, has it masked. ops/setup_ibkr_flex.py stores the token
and query ID in .runtime/ibkr-flex.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_URL = f"{BASE_URL}/SendRequest"
GET_URL = f"{BASE_URL}/GetStatement"
USER_AGENT = "Python/3.12 contest-hedge"
TIMEOUT = 30
RATE_LIMITED = 1018  # the next request waits out the per-minute window
# Not ready, busy or rate limited: ask again later. Every other code (token, query, IP, account) is fatal.
RETRY_CODES = frozenset({1001, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1018, 1019, 1021})
SETTINGS_FILE = Path(".runtime/ibkr-flex.json")
TOKEN = re.compile(r"[0-9]{10,}")
QUERY_ID = re.compile(r"[0-9]+")
NAV_FIELDS = {
    "total": "total",
    "cash": "cash",
    "stock": "stock",
    "options": "options",
    "interest_accruals": "interestAccruals",
}


class FlexError(Exception):
    """A Flex Web Service failure. `code` is IBKR's ErrorCode (None for HTTP, network or format
    failures); `retry` says whether asking again later can succeed. Never carries the token."""

    def __init__(self, message, code=None, retry=False):
        super().__init__(message)
        self.code, self.retry = code, retry


def redact(text):
    """`text` (a URL or a log line) with every t= query value masked."""
    return re.sub(r"([?&]t=)[^&#\s\"'<>]+", r"\1***", str(text))


class _RedactFlexLog(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "FlexWebService" in message:
            record.msg, record.args = redact(message), ()
        return True


logging.getLogger("httpx").addFilter(_RedactFlexLog())  # httpx logs every request URL at INFO


async def fetch_statement(
    token,
    query_id,
    *,
    transport=None,
    sleep=asyncio.sleep,
    attempts=6,
    wait_seconds=5,
    from_date=None,
    to_date=None,
):
    """The Flex Query's report as XML text. SendRequest, then GetStatement, waiting `wait_seconds`
    (at least 1 s; 6.7 s when more than ten requests could follow; 60 s after a 1018 rate limit)
    before each request, and asking again while IBKR answers a retry code, at most `attempts`
    requests after SendRequest. FlexError(retry=True) when it is still not ready or the service
    fails transiently; FlexError(retry=False) on a fatal code (expired token, invalid query, IP not
    allowed, ...) or a report that is not XML. from_date/to_date (dates, both or neither) override
    the query's period (SendRequest fd/td)."""
    token, query_id = str(token or "").strip(), str(query_id or "").strip()
    if not token or not query_id:
        raise FlexError("IBKR Flex token and query ID are required")
    if (from_date is None) != (to_date is None):
        raise FlexError("IBKR Flex needs both a from and a to date, or neither")
    period = {"fd": f"{from_date:%Y%m%d}", "td": f"{to_date:%Y%m%d}"} if from_date else {}
    wait = max(float(wait_seconds), 1.0, 6.7 if attempts >= 10 else 0.0)
    url, reference, code = SEND_URL, None, None
    async with httpx.AsyncClient(
        transport=transport, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as client:
        for attempt in range(attempts + 1):
            if attempt:
                await sleep(max(wait, 60.0) if code == RATE_LIMITED else wait)
            text, root = await _get(
                client, url, token, reference or query_id, reference is not None, {} if reference else period
            )
            if root.tag == "FlexQueryResponse" and reference is not None:
                return text
            if root.tag != "FlexStatementResponse":
                raise FlexError("IBKR Flex returned an unexpected XML document", retry=True)
            code = _int(root.findtext("ErrorCode"))
            if reference is None and code is None and (root.findtext("Status") or "").strip() == "Success":
                reference = (root.findtext("ReferenceCode") or "").strip()
                if not reference:
                    raise FlexError("IBKR Flex accepted the request without a reference code", retry=True)
                url = _statement_url(root.findtext("Url"))
                continue
            if code not in RETRY_CODES:
                raise FlexError(_failure(root, code, token), code=code, retry=False)
    raise FlexError(
        f"IBKR Flex statement not ready after {attempts} retries (code {code})", code=code, retry=True
    )


async def _get(client, url, token, q, report, extra=None):
    failure = None
    try:
        response = await client.get(url, params={"t": token, "q": q, "v": "3", **(extra or {})})
    except httpx.HTTPError as exc:  # its message and request carry the URL: raised outside, unchained
        failure = type(exc).__name__
    if failure:
        raise FlexError(f"IBKR Flex service unreachable ({failure})", retry=True)
    if not response.is_success:  # a redirect (maintenance page) is not followed: the token is in the URL
        status = response.status_code
        retry = status < 400 or status in (408, 429) or status >= 500
        raise FlexError(f"IBKR Flex request failed (HTTP {status})", retry=retry)
    text = response.text
    if not text.strip():
        raise FlexError("IBKR Flex returned an empty response", retry=True)
    if not text.lstrip("\ufeff \t\r\n").startswith("<"):
        if not report:
            raise FlexError("IBKR Flex returned a non-XML response", retry=True)
        raise FlexError("IBKR Flex returned a non-XML report: set the Flex Query's Format to XML")
    try:
        return text, _xml(text)
    except ValueError:
        raise FlexError("IBKR Flex returned an unreadable or unsafe XML response", retry=True) from None


def _statement_url(value):
    """SendRequest's GetStatement URL, unless it leaves https://*.interactivebrokers.com."""
    try:
        url = httpx.URL((value or "").strip())
    except (httpx.InvalidURL, TypeError):
        return GET_URL
    host = url.host or ""
    ibkr = host == "interactivebrokers.com" or host.endswith(".interactivebrokers.com")
    if url.scheme == "https" and ibkr and url.path.endswith("/GetStatement"):
        return f"https://{host}{url.path}"
    return GET_URL


def _failure(root, code, token):
    message = " ".join((root.findtext("ErrorMessage") or "").split()).replace(token, "***")[:200]
    if code is None:
        status = " ".join((root.findtext("Status") or "").split())[:40] or "unknown"
        return f"IBKR Flex request failed (status {status}{': ' + message if message else ''})"
    return f"IBKR Flex error {code}" + (f": {message}" if message else "")


def _xml(text):
    """The parsed root of an XML document without a DOCTYPE or ENTITY declaration, else ValueError."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("Refusing an XML document with a DOCTYPE or ENTITY declaration")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Unreadable XML ({exc})") from None


def _decimal(value):
    try:
        number = Decimal((value or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _int(value):
    number = _decimal(value)
    return int(number) if number is not None else None


def _date(value):
    """yyyyMMdd or yyyy-MM-dd (a ;HHmmss time is ignored), else None."""
    try:
        return date.fromisoformat((value or "").split(";")[0].strip())
    except ValueError:
        return None


def parse_statement(xml_text):
    """The first FlexStatement of a Flex report: {"account", "from", "to", "nav" {report date: {"total",
    "cash", "stock", "options", "interest_accruals"}} from EquitySummaryByReportDateInBase,
    "ending_value", "starting_value", "ending_date" from ChangeInNAV, "positions" (OpenPosition
    summary rows)}. Money is Decimal; a missing or unreadable attribute is None. ValueError for an
    unsafe or unreadable document, one without a FlexStatement, or one whose toDate is not yyyyMMdd
    or yyyy-MM-dd (another Date Format would silently drop every row)."""
    root = _xml(xml_text)
    statement = root if root.tag == "FlexStatement" else root.find(".//FlexStatement")
    if statement is None:
        raise ValueError("No FlexStatement in the IBKR Flex report")
    to = _date(statement.get("toDate"))
    if to is None and (statement.get("toDate") or "").strip():
        raise ValueError("Unreadable IBKR Flex date: set the Flex Query's Date Format to yyyyMMdd")
    nav = {}
    for row in statement.iter("EquitySummaryByReportDateInBase"):
        day = _date(row.get("reportDate"))
        if day is not None:
            nav[day] = {key: _decimal(row.get(name)) for key, name in NAV_FIELDS.items()}
    change = statement.find(".//ChangeInNAV")
    change = {} if change is None else change.attrib
    positions = []
    for row in statement.iter("OpenPosition"):
        conid = _int(row.get("conid"))
        if conid is None or (row.get("levelOfDetail") or "SUMMARY").strip().upper() != "SUMMARY":
            continue  # lot rows repeat their summary row
        strike, multiplier = _decimal(row.get("strike")), _int(row.get("multiplier"))
        positions.append(
            {
                "conid": conid,
                "symbol": (row.get("symbol") or "").strip(),
                "asset": (row.get("assetCategory") or "").strip(),
                "put_call": (row.get("putCall") or "").strip() or None,
                "strike": float(strike) if strike is not None else None,
                "expiry": _date(row.get("expiry")),
                "multiplier": multiplier or 1,
                "quantity": _decimal(row.get("position")),
                "mark": _decimal(row.get("markPrice")),
                "value": _decimal(row.get("positionValue")),
                "report_date": _date(row.get("reportDate")),
            }
        )
    return {
        "account": (statement.get("accountId") or "").strip(),
        "from": _date(statement.get("fromDate")),
        "to": to,
        "nav": nav,
        "ending_value": _decimal(change.get("endingValue")),
        "starting_value": _decimal(change.get("startingValue")),
        "ending_date": _date(change.get("toDate")) or (to if change else None),
        "positions": positions,
    }


def read_flex_settings(root):
    """(token, query_id) from root/.runtime/ibkr-flex.json when the file is owner-only, not a
    symbolic link and holds a valid token (10+ digits) and query ID (digits), else None."""
    path = Path(root) / SETTINGS_FILE
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return None
        settings = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    token = str(settings.get("token") or "").strip()
    query_id = str(settings.get("query_id") or "").strip()
    if TOKEN.fullmatch(token) and QUERY_ID.fullmatch(query_id):
        return token, query_id
    return None

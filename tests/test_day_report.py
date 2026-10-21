import asyncio
import json
import re
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from hedge.config import NY
from hedge.day_report import CONTRACT_FIELDS, DayReportBusy, ai_payload, attribute_day
from hedge.models import Execution, digest
from hedge.service import TradingService
from hedge.storage import Store
from hedge.supervisor import ADVISORY
from hedge.webapp import create_app

SECRET = "test-credential-5150"
D0, P, D = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)
BELL = "IBKR API closing observation (NetLiquidation; official close)"
POST = "IBKR API post-close observation (after 16:15 options close; NetLiquidation; official close)"
HEDGE_REF, BURN_IN, BURN_OUT = "hedge:" + "a" * 32, "b" * 32, "c" * 32


def contest_of(manifest):
    return manifest.model_copy(
        update={
            "contest_rules": "spcx_rklb_spy",
            "scoring_rule": "calendar_interest_benchmark",
            "scoring_annual_rate": Decimal("0.0344"),
        }
    )


def make_service(tmp_path, monkeypatch, manifest, handler=None, enabled=False, store=None):
    monkeypatch.chdir(tmp_path)
    if enabled:
        monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    if store is None:
        store = Store(f"sqlite:///{tmp_path}/day.db")
        store.initialize()
    service = TradingService(contest_of(manifest), store, demo=True)

    def forbidden(request):
        raise AssertionError("AI off: Google must not be called")

    service.supervisor.transport = httpx.MockTransport(handler or forbidden)
    return service


def et(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


def quote(mid, kind="OPT", spread=0.10, spot=195.0, iv=None, greeks=(0.0, 0.0, 0.0, 0.0), data_type="live"):
    delta, gamma, vega, theta = greeks if kind == "OPT" else (1.0, 0.0, 0.0, 0.0)
    return {
        "label": "x",
        "kind": kind,
        "bid": round(mid - spread / 2, 4),
        "ask": round(mid + spread / 2, 4),
        "mid": mid,
        "data_type": data_type,
        "provider": "moomoo",
        "observed_at": "2026-10-07T20:00:00+00:00",
        "spot": spot,
        "iv": iv,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "model_price": None,
    }


def marks(day, positions, quotes, ibkr, nav="1000000.00", cash="900000.00"):
    return {
        "date": str(day),
        "at": et(day, 16, 1).isoformat(),
        "nav": nav,
        "cash": cash,
        "accrued_cash": "100.00",
        "positions": {str(i): q for i, q in positions.items()},
        "quotes": {str(i): q for i, q in quotes.items()},
        "ibkr_marks": {
            str(i): {"market_price": p, "market_value": None, "position": 1} for i, p in ibkr.items()
        },
    }


PREV_MARKS = marks(
    P,
    {10: -100, 101: 25, 102: 25, 103: 10},
    {
        10: quote(195.0, "STK", 0.02),
        101: quote(5.00, iv=0.30, greeks=(0.52, 0.04, 0.20, -0.08)),
        102: quote(4.80, iv=0.31, greeks=(-0.48, 0.04, 0.20, -0.07)),
        103: quote(7.90, iv=0.29, greeks=(0.70, 0.03, 0.17, -0.07)),
        104: quote(2.60, iv=0.30, greeks=(0.30, 0.03, 0.15, -0.06)),
    },
    {10: 195.01, 101: 5.02, 102: 4.79, 103: 7.92},
)
CLOSE_MARKS = marks(
    D,
    {10: -100, 101: 25, 102: 25, 104: 5},
    {
        10: quote(196.50, "STK", 0.02, spot=196.5),
        101: quote(5.85, spot=196.5, iv=0.31, greeks=(0.57, 0.04, 0.20, -0.08)),
        102: quote(4.20, spot=196.5, iv=0.305, greeks=(-0.43, 0.04, 0.20, -0.07)),
        103: quote(8.95, spot=196.5, iv=0.29, greeks=(0.75, 0.03, 0.17, -0.07)),
        104: quote(3.20, spot=196.5, iv=0.30, greeks=(0.35, 0.03, 0.15, -0.06)),
    },
    {10: 196.49, 101: 5.86, 102: 4.21, 104: 3.21},
    nav="1000140.00",
)


def fill(service, exec_id, ref, con_id, quantity, price, at, commission=None):
    account = service.manifest.account
    service.store.execution(
        Execution(
            exec_id=exec_id,
            order_id=1,
            order_ref=ref,
            account=account,
            con_id=con_id,
            signed_quantity=quantity,
            price=Decimal(str(price)),
            at=at,
        )
    )
    if commission is not None:
        service.store.fee(account, exec_id, commission)


def at_clock(service, moment, write):
    """Write records stamped at `moment` (decisions are found by their record time)."""
    clock = service.store.clock
    service.store.clock = lambda: moment
    try:
        write()
    finally:
        service.store.clock = clock


def mark_id(service, day, phase):
    return digest((service.manifest.contest_id, str(day), phase))[:32]


def seed(service, *, prev_marks=True, close_marks=True, d0=False, pending_fee=False, nav_d="1000140.00"):
    """Scored day D=Oct 8 against P=Oct 7: three fill buckets, a burner round trip, a decision."""
    m, account = service.manifest, service.manifest.account
    if d0:
        service.ledger.import_close(D0, Decimal("999000.00"), BELL, account)
    service.ledger.import_close(P, Decimal("1000000.00"), POST, account)
    service.ledger.import_close(D, Decimal(nav_d), POST, account)
    if prev_marks:
        service.store.record("close_marks", account, PREV_MARKS, mark_id(service, P, "post_close"))
    if close_marks:
        service.store.record("close_marks", account, CLOSE_MARKS, mark_id(service, D, "post_close"))
    fill(service, "EXECID0", "", 10, 1, 194.0, et(P, 15))  # before P's close: not in D's window
    fill(service, "EXECID1", HEDGE_REF, 104, 5, 3.10, et(D, 11), 3.25)
    fill(service, "EXECID2", "", 103, -10, 8.50, et(D, 12), None if pending_fee else 6.50)
    fill(service, "EXECID3", "hedge:" + BURN_IN, 10, 100, 196.20, et(D, 15, 45), 1.00)
    fill(service, "EXECID4", "hedge:" + BURN_OUT, 10, -100, 196.16, et(D, 15, 46), 1.00)
    result = {
        "cycle_id": "d" * 32,
        "session": str(D),
        "status": "CLOSED",
        "con_id": 10,
        "label": "BA",
        "kind": "STK",
        "transmitted": True,
        "residual": 0,
        "realized_loss": 6.0,
        "fees": 2.0,
        "fees_pending": False,
        "intent_ids": [BURN_IN, BURN_OUT],
    }
    service.store.record("burn_cycle_result", account, result, "result:" + "d" * 32)
    decision = {
        "at": et(D, 15, 30).isoformat(),
        "status": "OPTIMAL",
        "trade_eligible": False,
        "blocking_reasons": ["Modeled improvement $1.50 does not exceed threshold $100.00"],
        "proposed_distance": 20.0,
        "nav_gap": 12.0,
        "quantities": {"101": 25},
    }
    at_clock(service, et(D, 15, 30), lambda: service.store.record("decision", account, decision))
    at_clock(
        service, et(P, 15, 30), lambda: service.store.record("decision", account, {**decision, "at": "x"})
    )
    return m


def amounts(report):
    return {d["key"]: d["amount"] for d in report["drivers"]}


def cents(value):
    return Decimal(str(value))


def test_attribution_identity_matches_the_official_score(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    seed(service)
    report, new = service.day_reports.compute(D, "operator")
    assert new and report["status"] == "complete" and report["checks"]["inventory_ok"] is True
    lines = amounts(report)
    attributed = [k for k in lines if k not in ("nav_change", "benchmark_growth", "miss")]
    assert sum(cents(lines[k]) for k in attributed) == cents(lines["nav_change"]) == Decimal("140.00")
    assert cents(lines["nav_change"]) - cents(lines["benchmark_growth"]) == cents(lines["miss"])
    target = service.ledger.target_for(D, Decimal("1000000.00"))
    assert cents(lines["miss"]) == (Decimal("1000140.00") - target).quantize(Decimal("0.01"))
    ledger_day = next(x for x in service.ledger.summary()["days"] if x["date"] == str(D))
    assert (
        report["official"]["score"] == ledger_day["score"] and Decimal(report["official"]["target"]) == target
    )
    assert report["official"]["provenance"] == "api" and report["official"]["accrual_days"] == 1
    # Each bucket, marked to IBKR's close marks where held at both closes.
    assert lines["burner"] == -6.0 and report["checks"]["burner_realized_cost"] == 6.0
    assert lines["hedge_trades"] == 55.0  # +5 x 100 x (3.21 - 3.10)
    assert lines["manual_trades"] == -450.0  # -10 x 100 x (8.95 mid - 8.50): no longer held
    assert lines["fees"] == -9.75
    assert lines["interest"] == round(-100 * 195.01 * 0.05 / 360, 2)
    assert report["by_symbol"]["BA"]["delta"] is not None and report["inputs"]["fills"] == 4
    assert report["inputs"]["marks_prev"]["phase"] == "post_close"
    assert report["context"]["decisions"]["count"] == 1
    assert report["context"]["decisions"]["top_blocking_reasons"][0]["reason"] == (
        "Modeled improvement # does not exceed threshold #"
    )
    assert report["context"]["forecast"]["proposed_distance"] == 20.0
    for row in report["contracts"]:  # no quantities in the per-contract rows
        assert not {"position", "positions", "quantity", "signed_quantity"} & set(row)
    again, new = service.day_reports.compute(D, "operator")
    assert not new and again["id"] == report["id"]
    assert len(service.store.records("day_report", service.manifest.account)) == 1


def pure(manifest, **overrides):
    contest = contest_of(manifest)
    args = {
        "manifest": contest,
        "day": D,
        "previous_day": P,
        "official": {"nav": Decimal("1001000.00"), "source": POST, "provenance": "api"},
        "previous_official": {"nav": Decimal("1000000.00"), "source": POST, "provenance": "api"},
        "target": Decimal("1000095.56"),
        "marks_prev": marks(
            P, {101: 10}, {101: quote(5.00, spot=100, iv=0.30, greeks=(0.5, 0.04, 0.2, -0.05))}, {}
        ),
        "marks_close": marks(D, {101: 10}, {101: quote(6.30, spot=102, iv=0.32)}, {}),
        "fills": [],
        "burn_results": [],
        "decisions": [],
    }
    return attribute_day(**{**args, **overrides})


def test_option_greek_split_is_exact(manifest):
    report = pure(manifest)
    lines = amounts(report)
    # q=10, m=100: delta 0.5 x 2, gamma 0.5 x 0.04 x 4, vega 0.2 x 0.02, theta -0.05 x 1 day.
    assert (lines["delta"], lines["gamma"], lines["vega"], lines["theta"]) == (1000.0, 80.0, 4.0, -50.0)
    assert lines["higher_order"] == 1300.0 - 1034.0 and lines["mark_basis"] == 0.0
    assert report["contracts"][0]["mark_source"] == "mid"
    assert [a["code"] for a in report["anomalies"]] == ["large_higher_order"]  # |266| > 200


def test_ibkr_mark_outside_the_quote_is_an_anomaly(manifest):
    report = pure(
        manifest,
        marks_prev=marks(P, {101: 10}, {101: quote(5.00, spot=100, iv=0.3)}, {101: 5.05}),
        marks_close=marks(D, {101: 10}, {101: quote(6.00, spot=100, iv=0.3)}, {101: 6.40}),
    )
    outside = [a for a in report["anomalies"] if a["code"] == "ibkr_mark_outside_quote"]
    assert (
        len(outside) == 1 and "today's close" in outside[0]["detail"] and outside[0]["item"].startswith("BA")
    )


def test_mark_basis_line(manifest):
    report = pure(
        manifest,
        marks_prev=marks(P, {101: 10}, {101: quote(5.00, spot=100, iv=0.3)}, {101: 5.05}),
        marks_close=marks(D, {101: 10}, {101: quote(6.00, spot=100, iv=0.3)}, {101: 6.40}),
    )
    lines = amounts(report)
    assert lines["mark_basis"] == 350.0  # 10 x 100 x [(6.40 - 6.00) - (5.05 - 5.00)]
    assert lines["higher_order"] == 1000.0 and report["contracts"][0]["mark_source"] == "ibkr"
    assert report["contracts"][0]["pnl"] == 1350.0  # = 10 x 100 x (6.40 - 5.05)


def test_day1_without_day0_marks_degrades_to_totals(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    account = service.manifest.account
    service.ledger.import_close(D0, Decimal("1000000.00"), BELL, account)
    service.ledger.import_close(P, Decimal("1000300.00"), BELL, account)
    day1 = {**PREV_MARKS, "date": str(P)}  # Day 1's own close marks exist; Day 0's do not
    service.store.record("close_marks", account, day1, mark_id(service, P, "pre_close"))
    report, _ = service.day_reports.compute(P, "operator")
    lines = amounts(report)
    assert report["status"] == "partial" and report["previous_date"] == str(D0)
    assert all(lines[k] is None for k in ("delta", "gamma", "vega", "theta", "higher_order", "mark_basis"))
    assert lines["interest"] is None and lines["unexplained"] == 300.0
    assert any("Close marks for 2026-10-06 are missing" in n for n in report["notes"])
    unexplained = next(d for d in report["drivers"] if d["key"] == "unexplained")
    assert "previous close marks missing" in unexplained["label"]
    assert report["checks"]["inventory_ok"] is None


def test_missing_close_marks_degrades(manifest):
    burn = {"con_id": 10, "realized_loss": 6.0, "intent_ids": ["x"], "transmitted": True, "status": "CLOSED"}
    fills = [
        {"con_id": 104, "signed_quantity": 5, "price": Decimal("3.10"), "commission": Decimal("3.25"), "order_ref": HEDGE_REF},
        {"con_id": 10, "signed_quantity": 100, "price": Decimal("196"), "commission": None, "order_ref": "hedge:x"},
    ]  # fmt: skip
    report = pure(manifest, marks_close=None, fills=fills, burn_results=[burn])
    lines = amounts(report)
    assert report["status"] == "partial"
    assert lines["delta"] is None and lines["hedge_trades"] is None and lines["manual_trades"] is None
    assert lines["burner"] == -6.0 and lines["fees"] == -3.25  # burner from the cycle's realized loss
    assert report["checks"]["fees_estimated"] == 0  # burner fills are not re-estimated here
    assert any("Close marks for 2026-10-08 are missing" in n for n in report["notes"])
    assert "close marks missing" in next(d for d in report["drivers"] if d["key"] == "unexplained")["label"]


def test_pending_commissions_are_estimated(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    seed(service, pending_fee=True)
    report, _ = service.day_reports.compute(D, "operator")
    assert report["status"] == "partial" and report["checks"]["fees_estimated"] == 1
    assert amounts(report)["fees"] == -3.25 - 6.50  # option_commission(10) = max(1, 0.65 x 10)
    assert "fees_estimated" in [a["code"] for a in report["anomalies"]]
    service.store.fee(service.manifest.account, "EXECID2", 7.00)
    now = et(D, 16, 30)
    service.day_reports.tick(now)  # same inputs (fees excluded): no new automatic report
    assert len(service.store.records("day_report", service.manifest.account)) == 1
    recomputed, new = service.day_reports.compute(D, "operator")  # Recompute now picks the report up
    assert new and recomputed["status"] == "complete" and amounts(recomputed)["fees"] == -10.25


def test_no_report_without_the_official_close(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    with TestClient(create_app(service)) as client:
        page = client.get("/").text
        assert 'id="day-report-panel"' in page and "/api/export/day_reports" in page
        token = re.search(r'name="control-token" content="([^"]+)"', page).group(1)
        assert client.post("/api/day-report", json={}).status_code == 403
        headers = {"X-Control-Token": token}
        early = client.post("/api/day-report", json={}, headers=headers)  # demo clock: before any close
        assert early.status_code == 409 and "No scored session has closed yet" in early.json()["detail"]
        service.day_reports.compute_limit.hits.clear()
        waiting = client.post("/api/day-report", json={"date": str(P)}, headers=headers)
        assert (
            waiting.status_code == 409
            and waiting.json()["detail"] == f"Waiting for the official close for {P}"
        )
        assert client.post("/api/day-report", json={"date": str(P)}, headers=headers).status_code == 429
        service.day_reports.compute_limit.hits.clear()
        bad = client.post("/api/day-report", json={"date": "2026-10-10"}, headers=headers)
        assert bad.status_code == 422
        extra = client.post("/api/day-report", json={"date": str(P), "apply": True}, headers=headers)
        assert extra.status_code == 422
        off = client.post("/api/day-report", json={"date": str(P), "action": "explain"}, headers=headers)
        assert off.status_code == 409 and "AI is off" in off.json()["detail"]
    assert service.store.records("day_report", service.manifest.account) == []


def test_report_runs_once_after_settle_and_again_after_a_correction(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    seed(service)
    account = service.manifest.account

    def reports():
        return service.store.records("day_report", account, 10)

    def ready_alerts():
        return [a for a in service.store.records("alert", account, 20) if "Day-end report" in a["message"]]

    assert service.day_reports.settle(D) == et(D, 16, 26)
    service.day_reports.tick(et(D, 16, 25))
    assert reports() == []
    service.day_reports.tick(et(D, 16, 27))
    assert len(reports()) == 1 and reports()[0]["trigger"] == "automatic"
    assert len(ready_alerts()) == 1 and ready_alerts()[0]["severity"] == "info"
    assert ready_alerts()[0]["message"].startswith(f"Day-end report for {D} ready: miss ")
    service.day_reports.tick(et(D, 16, 28))
    restarted = make_service(tmp_path, monkeypatch, manifest, store=service.store)
    restarted.day_reports.load()
    restarted.day_reports.tick(et(D, 16, 40))
    assert len(reports()) == 1 and len(ready_alerts()) == 1
    restarted.ledger.import_close(D, Decimal("1000150.00"), "Manual correction (IBKR closing NAV)", account)
    restarted.day_reports.tick(et(D, 16, 45))
    assert len(reports()) == 2 and len(ready_alerts()) == 2
    newest = restarted.day_reports.latest()[0]
    assert newest["official"]["nav"].startswith("1000150") and newest["official"]["provenance"] == "statement"
    # A manual close is not the post-close observation: the post-close marks are used, with a note.
    assert any(n.startswith("Used the post-close marks for 2026-10-08") for n in newest["notes"])


def reply(**fields):
    body = {
        "date": str(D),
        "explanation": "Stock moves drove most of the miss; the unexplained part is small.",
        "anomalies": [{"item": "BA 2026-10-16 200C", "why": "Wide quote", "check": "Compare IBKR's mark"}],
        "suggestions": [{"setting": "no_change", "reason": "Nothing points to a change."}],
        **fields,
    }
    return {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(body)}]}],
        "usage": {"total_tokens": 11},
    }


async def test_ai_explanation_payload_and_menu(tmp_path, monkeypatch, manifest):
    sent = []
    replies = [
        reply(),
        reply(suggestions=[{"setting": "place_order", "reason": "x"}]),
        reply(extra="field"),
        reply(date="2026-10-07"),
        reply(
            suggestions=[{"setting": "no_change", "reason": "a"}, {"setting": "burner_off", "reason": "b"}]
        ),
    ]

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    service = make_service(tmp_path, monkeypatch, manifest, handler, enabled=True)
    seed(service)
    report, _ = service.day_reports.compute(D, "operator")

    def trading():
        return (
            service.mode,
            service.profile,
            service.control_epoch,
            service.autostart_mode(),
            service.burner.override,
        )

    before = trading()
    annex = await service.day_reports.explain(report["id"], "operator")
    assert annex["status"] == "ok" and annex["advisory_only"] is True and annex["model"] == "gemini-3.8-flash"
    assert annex["suggestions"] == [
        {"setting": "no_change", "label": "No change", "reason": "Nothing points to a change."}
    ]
    body = sent[0]
    assert body["tools"] == [] and body["store"] is False and body["system_instruction"].endswith(ADVISORY)
    assert (
        "IBKR API closing NAV" in body["system_instruction"]
        and "(1 + 0.0344/360)^n_i" in body["system_instruction"]
    )
    raw = body["input"]
    for private in ("DU_TEST", "EXECID", "hedge:", HEDGE_REF, BURN_IN, "positions", "signed_quantity", '"position"', "quantities", SECRET):  # fmt: skip
        assert private not in raw, private
    data = json.loads(raw)
    assert data["date"] == str(D) and data["allowed_settings"]["burner_off"] == "Burner off"
    assert {d["label"] for d in data["drivers"]} >= {"Closing burner (fills and fees)", "Time decay"}
    for _ in range(4):  # off-menu setting, extra field, wrong date, no_change with others: rejected
        failed = await service.day_reports.explain(report["id"], "operator")
        assert failed["status"] == "failed" and failed["error"] == "Invalid Google AI response rejected"
        assert failed["suggestions"] == [] and failed["explanation"] is None
    alerts = [a for a in service.store.records("alert", service.manifest.account, 20) if "AI" in a["message"]]
    assert len(alerts) == 1 and alerts[0]["message"].startswith("Day-end report AI unavailable")
    assert len(service.store.records("day_report_ai", service.manifest.account, 10)) == 5
    assert trading() == before
    view = service.day_reports.latest()[0]
    assert view["ai"]["status"] == "failed" and "contracts" not in view


async def test_automatic_explanation_runs_once_for_the_newest_day(tmp_path, monkeypatch, manifest):
    calls = []

    def handler(request):
        calls.append(1)  # echo the requested date, as a valid reply must
        return httpx.Response(200, json=reply(date=json.loads(json.loads(request.content)["input"])["date"]))

    service = make_service(tmp_path, monkeypatch, manifest, handler, enabled=True)
    seed(service, d0=True)
    service.store.record(
        "close_marks", service.manifest.account, {**PREV_MARKS}, mark_id(service, D0, "pre_close")
    )
    service.day_reports.tick(et(D, 16, 27))
    assert {r["date"] for r in service.day_reports.latest()} == {str(P), str(D)}
    await asyncio.gather(*service.ai_tasks)
    service.day_reports.tick(et(D, 16, 28))
    await asyncio.gather(*service.ai_tasks)
    assert len(calls) == 1  # only the newest day, and only once
    latest = service.day_reports.latest()
    assert latest[0]["date"] == str(D) and latest[0]["ai"]["status"] == "ok"
    assert latest[1]["ai"] is None and latest[1]["ai_note"].startswith("No AI explanation yet")
    started = service.day_reports.request(P, "explain")  # older days: the operator's button
    assert started["status"] == "started"
    await asyncio.gather(*service.ai_tasks)
    assert len(calls) == 2 and service.day_reports.latest()[1]["ai"]["status"] == "ok"
    with pytest.raises(ValueError, match="Please wait"):
        service.day_reports.request(P, "explain")


def test_numbers_visible_with_ai_off(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    seed(service)
    service.day_reports.tick(et(D, 16, 27))
    state = service.state()
    report = state["day_reports"][0]
    assert report["drivers"] and report["ai"] is None and report["ai_note"] == "AI off: numbers only"
    assert state["ai_features"]["day_report"]["enabled"] is False
    assert "AI off" in state["ai_features"]["day_report"]["status"]
    with pytest.raises(DayReportBusy, match="AI is off"):
        service.day_reports.request(D, "explain")
    context = service.qa.context(state)  # the Q&A sees the summary only (numbers and labels)
    summary = context["state"]["day_report"]
    assert summary["date"] == str(D) and "day_report" in context["state_keys"]
    assert (
        set(summary) == {"date", "miss", "score", "status", "top_drivers"}
        and len(summary["top_drivers"]) == 3
    )


def test_export_day_reports_csv(tmp_path, monkeypatch, manifest):
    formula = '=HYPERLINK("https://example.invalid")'
    service = make_service(
        tmp_path,
        monkeypatch,
        manifest,
        lambda r: httpx.Response(200, json=reply(explanation=formula)),
        enabled=True,
    )
    seed(service)
    report, _ = service.day_reports.compute(D, "operator")
    asyncio.run(service.day_reports.explain(report["id"], "operator"))
    with TestClient(create_app(service)) as client:
        response = client.get("/api/export/day_reports")
    assert response.status_code == 200
    text = response.text
    assert "'=HYPERLINK" in text and "\n=HYPERLINK" not in text and ",=HYPERLINK" not in text
    header = text.splitlines()[0].split(",")
    assert {"date", "scope", "driver", "amount_usd", "score", "report_id", "text", "suggestions"} <= set(
        header
    )
    assert "Portfolio" in text and ",BA," in text and ",AI," in text
    # The report's own total lines are not formula-guarded: no visible apostrophe.
    assert (
        ",Official NAV change (total)," in text and ",Miss (official close − target); score = |miss|," in text
    )
    assert "'= " not in text


def test_ai_payload_rows_never_give_quantities_back(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    seed(service)
    report, _ = service.day_reports.compute(D, "operator")
    assert any(row["pnl"] for row in report["contracts"])  # the recorded report keeps per-contract P&L
    data = json.loads(json.dumps(ai_payload(report, service.manifest.account), default=str))
    assert data["contracts"] and data["by_symbol"]["BA"]["total"] is not None  # P&L at symbol level only
    for row in data["contracts"]:
        # pnl / (multiplier x mark change) would give the quantity: no P&L and no previous-close marks.
        assert set(row) == set(CONTRACT_FIELDS)
        assert not {"pnl", "higher_order", "mark_basis", "ibkr_prev", "mid_prev"} & set(row)


async def test_an_explanation_after_ai_is_turned_off_is_dropped(tmp_path, monkeypatch, manifest):
    holder = []

    def handler(request):
        holder[0].supervisor.configure(enabled=False)  # the operator presses Disable AI meanwhile
        return httpx.Response(200, json=reply())

    service = make_service(tmp_path, monkeypatch, manifest, handler, enabled=True)
    holder.append(service)
    seed(service)
    report, _ = service.day_reports.compute(D, "operator")
    assert await service.day_reports.explain(report["id"], "operator") is None
    assert service.store.records("day_report_ai", service.manifest.account, 5) == []
    view = service.day_reports.latest()[0]
    assert view["ai"] is None and view["ai_note"] == "AI off: numbers only"
    status = service.day_reports.status()
    assert status["last_error"] is None and "turned off" in status["status"]
    assert not [
        a for a in service.store.records("alert", service.manifest.account, 10) if "AI" in a["message"]
    ]


def test_day_selector_offers_closed_sessions_without_a_report(tmp_path, monkeypatch, manifest):
    service = make_service(tmp_path, monkeypatch, manifest)
    service.now = lambda: et(D, 16, 5)  # D closed; its automatic report comes at 16:26
    state = service.state()
    assert state["day_reports"] == [] and state["day_report_sessions"] == [str(D), str(P)]
    service.now = lambda: et(D, 15, 59)
    assert service.state()["day_report_sessions"] == [str(P)]

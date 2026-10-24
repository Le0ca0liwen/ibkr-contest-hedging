import gzip
import json
from datetime import datetime, timezone
from decimal import Decimal

from hedge.config import NY
from hedge.scoring import ScoreLedger
from hedge.storage import Store
from ops.export_mark_dataset import main


def test_the_export_gathers_captures_statements_ledger_and_archived_closing_books(manifest, tmp_path):
    url = f"sqlite:///{tmp_path}/hedge.db"
    store = Store(url)
    store.initialize()
    day = manifest.day_zero
    store.record(
        "close_marks",
        manifest.account,
        {"date": str(day), "phase": "pre_close", "quotes": {"101": {"bid": 1}}},
    )
    store.record(
        "flex_statement", manifest.account, {"date": str(day), "positions": {"101": {"mark": "6.55"}}}
    )
    store.record("decision", manifest.account, {"date": str(day)})  # not exported
    ScoreLedger(store, manifest).import_close(day, Decimal("1000000"), "IBKR statement", manifest.account)
    archives = tmp_path / "moomoo"
    archives.mkdir()
    at = datetime.combine(day, datetime.min.time(), NY).replace(hour=15, minute=59, second=50)
    stamp = f"{at:%Y-%m-%d %H:%M:%S}.000"
    line = {
        "received_at": at.astimezone(timezone.utc).isoformat(),
        "securities": {
            "US.X": {
                "order_book": {
                    "Bid": [[6.0, 1, 1, []]],
                    "Ask": [[6.2, 1, 1, []]],
                    "svr_recv_time_bid": stamp,
                    "svr_recv_time_ask": stamp,
                },
                "book_push_count": 3,
            }
        },
    }
    with gzip.open(archives / f"raw-{day:%Y%m%d}.jsonl.gz", "wt") as f:
        f.write(json.dumps(line) + "\n")
    out = tmp_path / "dataset.json"
    main(["--database", url, "--archives", str(archives), "--output", str(out)])
    data = json.loads(out.read_text())["days"][str(day)]
    assert [r["phase"] for r in data["close_marks"]] == ["pre_close"]
    assert data["flex_statement"][0]["positions"]["101"]["mark"] == "6.55" and "decision" not in data
    assert data["ledger"][0]["nav"].startswith("1000000")
    assert data["archive"]["16:00"]["US.X"]["bid"] == 6.0 and data["archive"]["codes"] == ["US.X"]

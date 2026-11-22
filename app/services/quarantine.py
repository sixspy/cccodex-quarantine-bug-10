from __future__ import annotations

from app.db import get_db
from app.services import audit as audit_svc
from app.services import dedupe_ledger
from app.services import settings as settings_svc
from app.services.errors import ServiceError


def list_bins(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT b.*,
              (SELECT COUNT(*) FROM quarantine_rows r WHERE r.bin_id=b.id AND r.status='待复核') AS pending_count,
              (SELECT COUNT(*) FROM quarantine_rows r WHERE r.bin_id=b.id) AS row_count
            FROM quarantine_bins b ORDER BY b.id
            """
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            fps = conn.execute(
                """
                SELECT d.id AS ledger_id, d.quarantine_row_id, d.fingerprint,
                       d.source_key, d.capture_item_id
                FROM dedupe_ledger d
                JOIN quarantine_rows r ON r.id=d.quarantine_row_id
                WHERE r.bin_id=? AND d.fate='已落舱'
                ORDER BY d.id
                """,
                (r["id"],),
            ).fetchall()
            data["fingerprints"] = [
                {
                    "ledger_id": f["ledger_id"],
                    "quarantine_row_id": f["quarantine_row_id"],
                    "fingerprint": f["fingerprint"],
                    "fingerprint_short": dedupe_ledger.short_fp(f["fingerprint"]),
                    "source_key": f["source_key"],
                    "capture_item_id": f["capture_item_id"],
                }
                for f in fps
            ]
            data["dedupe_landing_count"] = len(fps)
            out.append(data)
        return out


def list_rows(bin_id: int | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if bin_id:
            rows = conn.execute(
                """
                SELECT r.*, i.item_key, i.quality, i.batch_id, b.bin_code, b.title AS bin_title
                FROM quarantine_rows r
                JOIN capture_items i ON i.id=r.item_id
                JOIN quarantine_bins b ON b.id=r.bin_id
                WHERE r.bin_id=?
                ORDER BY r.id DESC
                """,
                (bin_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT r.*, i.item_key, i.quality, i.batch_id, b.bin_code, b.title AS bin_title
                FROM quarantine_rows r
                JOIN capture_items i ON i.id=r.item_id
                JOIN quarantine_bins b ON b.id=r.bin_id
                ORDER BY r.id DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]


def enter_item(item_id: int, bin_id: int, reason: str, db_path: str | None = None) -> dict:
    reason = (reason or "").strip() or "质量异常入舱"
    refused: dedupe_ledger.DedupeRefused | None = None
    row_id = 0
    # 入舱与去重记账同一笔事务：先拿写锁串行化，避免同源件两路争抢互盖落点。
    with get_db(db_path, immediate=True) as conn:
        item = conn.execute(
            """
            SELECT i.*, b.source_name
            FROM capture_items i
            JOIN capture_batches b ON b.id=i.batch_id
            WHERE i.id=?
            """,
            (item_id,),
        ).fetchone()
        if not item:
            raise ServiceError("条目不存在", 404)
        if item["status"] not in ("待检",):
            raise ServiceError("仅待检条目可入舱")
        if item["quality"] == "合格":
            raise ServiceError("合格条目不应入舱")
        bin_row = conn.execute("SELECT * FROM quarantine_bins WHERE id=?", (bin_id,)).fetchone()
        if not bin_row:
            raise ServiceError("隔离舱不存在", 404)
        if bin_row["status"] != "开放":
            raise ServiceError("隔离舱未开放")
        open_bins = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_bins WHERE status='开放'"
        ).fetchone()["c"]
        max_open_row = conn.execute(
            "SELECT value FROM lab_settings WHERE key='max_bin_open'"
        ).fetchone()
        max_open = int((max_open_row["value"] if max_open_row else "3") or "3")
        if open_bins > max_open:
            raise ServiceError("开放隔离舱超过上限")
        occupied = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE bin_id=? AND status IN ('待复核','回炉中')",
            (bin_id,),
        ).fetchone()["c"]
        if occupied >= bin_row["capacity"]:
            raise ServiceError("隔离舱已满")
        exists = conn.execute(
            "SELECT id FROM quarantine_rows WHERE item_id=?", (item_id,)
        ).fetchone()
        if exists:
            raise ServiceError("条目已在隔离舱")
        skey = dedupe_ledger.source_key(item["source_name"], item["item_key"])
        landing = dedupe_ledger.assert_integrity(conn, skey)
        if landing:
            # 同源件已落点：本笔只落拒重，绝不二次创建隔离行。
            refused = dedupe_ledger.refuse_duplicate(conn, item, landing)
        else:
            cur = conn.execute(
                """
                INSERT INTO quarantine_rows(bin_id, item_id, reason, status)
                VALUES (?, ?, ?, '待复核')
                """,
                (bin_id, item_id, reason),
            )
            row_id = cur.lastrowid
            conn.execute(
                "UPDATE capture_items SET status='入舱' WHERE id=?", (item_id,)
            )
            n = conn.execute("SELECT COUNT(*) AS c FROM review_tickets").fetchone()["c"] + 1
            conn.execute(
                """
                INSERT INTO review_tickets(ticket_code, row_id, decision, comment)
                VALUES (?, ?, '待决', ?)
                """,
                (f"REV-{n:04d}", row_id, "入舱自动开复核单"),
            )
            dedupe_ledger.record_landing(conn, item, row_id)
    if refused:
        audit_svc.write_event(
            "拒重入舱",
            "capture_item",
            item_id,
            refused.message,
            db_path=db_path,
        )
        raise refused
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT r.*, i.item_key, i.quality, b.bin_code
            FROM quarantine_rows r
            JOIN capture_items i ON i.id=r.item_id
            JOIN quarantine_bins b ON b.id=r.bin_id
            WHERE r.id=?
            """,
            (row_id,),
        ).fetchone()
    audit_svc.write_event("入舱", "quarantine_row", row_id, reason, db_path=db_path)
    return dict(row)


def auto_enter_bad(batch_id: int, bin_id: int, db_path: str | None = None) -> dict:
    auto = settings_svc.get_value("auto_enter_bad", "1", db_path)
    if auto != "1":
        raise ServiceError("未开启自动入舱")
    with get_db(db_path) as conn:
        items = conn.execute(
            """
            SELECT id FROM capture_items
            WHERE batch_id=? AND quality!='合格' AND status='待检'
            """,
            (batch_id,),
        ).fetchall()
    entered = []
    refused = []
    for it in items:
        try:
            entered.append(enter_item(it["id"], bin_id, "批内自动入舱", db_path))
        except dedupe_ledger.DedupeRefused as exc:
            refused.append({"item_id": it["id"], "message": exc.message})
    return {
        "entered": len(entered),
        "rows": entered,
        "refused": len(refused),
        "refusals": refused,
    }

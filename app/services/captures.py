from __future__ import annotations

from app.db import get_db
from app.services import audit as audit_svc
from app.services import dedupe_ledger
from app.services.errors import ServiceError


def _collision_count(conn, source_name: str) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) AS c FROM dedupe_ledger d
        JOIN capture_items i ON i.id=d.capture_item_id
        JOIN capture_batches b ON b.id=i.batch_id
        WHERE b.source_name=? AND d.fate='拒重'
        """,
        (source_name,),
    ).fetchone()["c"]


def list_batches(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, batch_code, source_name, status, item_count, bad_count, note, created_at
            FROM capture_batches ORDER BY id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["collision_count"] = _collision_count(conn, data["source_name"])
            out.append(data)
        return out


def get_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM capture_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not row:
            raise ServiceError("批次不存在", 404)
        items = conn.execute(
            """
            SELECT id, item_key, quality, status, payload, created_at
            FROM capture_items WHERE batch_id=? ORDER BY id
            """,
            (batch_id,),
        ).fetchall()
        data = dict(row)
        data["items"] = []
        for i in items:
            item = dict(i)
            status = dedupe_ledger.status_for(
                conn, row["source_name"], item["item_key"], item["payload"]
            )
            item["dedupe_badge"] = status["badge"]
            item["collision_count"] = status["collision_count"]
            item["dedupe_landing"] = status["landing"]
            item["landing_row"] = status["landing_row"]
            data["items"].append(item)
        data["collision_count"] = _collision_count(conn, row["source_name"])
        return data


def create_batch(source_name: str, note: str = "", db_path: str | None = None) -> dict:
    source_name = (source_name or "").strip()
    if not source_name:
        raise ServiceError("来源名不能空")
    with get_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM capture_batches").fetchone()["c"] + 1
        code = f"CAP-NEW-{n:04d}"
        cur = conn.execute(
            """
            INSERT INTO capture_batches(batch_code, source_name, status, note)
            VALUES (?, ?, '采集中', ?)
            """,
            (code, source_name, note or ""),
        )
        batch_id = cur.lastrowid
    audit_svc.write_event("新建批次", "capture_batch", batch_id, code, db_path=db_path)
    return get_batch(batch_id, db_path)


def add_item(
    batch_id: int,
    item_key: str,
    payload: str = "",
    quality: str = "合格",
    db_path: str | None = None,
) -> dict:
    item_key = (item_key or "").strip()
    if not item_key:
        raise ServiceError("条目键不能空")
    if quality not in ("合格", "缺字段", "编码空", "重复键"):
        raise ServiceError("质量标记无效")
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM capture_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能再加条目")
        exists = conn.execute(
            "SELECT id FROM capture_items WHERE batch_id=? AND item_key=?",
            (batch_id, item_key),
        ).fetchone()
        if exists:
            raise ServiceError("同批键已存在")
        cur = conn.execute(
            """
            INSERT INTO capture_items(batch_id, item_key, payload, quality, status)
            VALUES (?, ?, ?, ?, '待检')
            """,
            (batch_id, item_key, payload or "", quality),
        )
        _refresh_batch_counts(conn, batch_id)
        item_id = cur.lastrowid
        row = conn.execute("SELECT * FROM capture_items WHERE id=?", (item_id,)).fetchone()
    audit_svc.write_event("追加条目", "capture_item", item_id, item_key, db_path=db_path)
    return dict(row)


def close_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM capture_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        conn.execute(
            "UPDATE capture_batches SET status='已关闭' WHERE id=?", (batch_id,)
        )
        conn.execute(
            "UPDATE capture_items SET status='已关闭' WHERE batch_id=? AND status='待检' AND quality='合格'",
            (batch_id,),
        )
    audit_svc.write_event("关闭批次", "capture_batch", batch_id, "", db_path=db_path)
    return get_batch(batch_id, db_path)


def _refresh_batch_counts(conn, batch_id: int) -> None:
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM capture_items WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    bad = conn.execute(
        "SELECT COUNT(*) AS c FROM capture_items WHERE batch_id=? AND quality!='合格'",
        (batch_id,),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE capture_batches SET item_count=?, bad_count=? WHERE id=?",
        (cnt, bad, batch_id),
    )

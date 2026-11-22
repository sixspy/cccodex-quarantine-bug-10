"""抓取去重：独立账册形态（甲侧）。

去重工程形态择定为「独立账册」：隔离舱入舱时在**同一笔事务**里把源件键与撞件指纹
记入本账册，落定成功才算入舱；抓取侧只读引用账册，不改写账册。

账册两笔命运：
- ``已落舱``：该源件首次入舱，记下落点的隔离行；同一源件键全库只允许一笔。
- ``拒重``：同源件重复抓取，拒绝二次入舱，记下撞件指纹与所属落点。
"""
from __future__ import annotations

import hashlib

from app.db import get_db
from app.services.errors import ServiceError

TITLE = "抓取去重账册"
FORM = "ledger"
FORM_LABEL = "独立账册"
FINGERPRINT_REF = "dedupe_ledger"
BADGE = "已去重"
LANDED = "已落舱"
REFUSED = "拒重"


class DedupeRefused(ServiceError):
    """同源件重复抓取：本笔拒重已落账，状态码 409。"""

    def __init__(self, message: str, landing_id: int | None = None, collision_count: int = 0):
        super().__init__(message, 409)
        self.landing_id = landing_id
        self.collision_count = collision_count


def source_key(source_name: str | None, item_key: str | None) -> str:
    return f"{(source_name or '').strip()}::{(item_key or '').strip()}"


def fingerprint(payload: str | None) -> str:
    """源件指纹：只认抓取内容，不认质量标记（质量是检疫结论，不是源件）。"""
    return hashlib.sha256(("cc-cap\0" + (payload or "")).encode("utf-8")).hexdigest()


def short_fp(value: str | None) -> str:
    return (value or "")[:12]


def policy() -> dict:
    return {
        "form": FORM,
        "form_label": FORM_LABEL,
        "fingerprint_ref": FINGERPRINT_REF,
        "read_only": True,
        "owner": "隔离舱入舱事务",
        "reader": "抓取侧只读",
        "note": "去重口径：源件键+撞件指纹记入独立账册；入舱与记账同一笔事务，抓取侧只读引用。",
    }


def _landing(conn, skey: str):
    return conn.execute(
        "SELECT * FROM dedupe_ledger WHERE source_key=? AND fate=? ORDER BY id LIMIT 1",
        (skey, LANDED),
    ).fetchone()


def _collisions(conn, skey: str):
    return conn.execute(
        "SELECT * FROM dedupe_ledger WHERE source_key=? AND fate=? ORDER BY id",
        (skey, REFUSED),
    ).fetchall()


def _public(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["fingerprint_short"] = short_fp(data.get("fingerprint"))
    data["badge"] = BADGE
    return data


def status_for(conn, source_name: str, item_key: str, payload: str | None) -> dict:
    """抓取侧只读取数：徽标、撞件次数、去重落点。"""
    skey = source_key(source_name, item_key)
    landing = _landing(conn, skey)
    collisions = _collisions(conn, skey)
    fp = fingerprint(payload)
    landing_row = None
    if landing and landing["quarantine_row_id"]:
        landing_row = conn.execute(
            """
            SELECT r.id, r.status, r.bin_id, b.bin_code
            FROM quarantine_rows r
            JOIN quarantine_bins b ON b.id=r.bin_id
            WHERE r.id=?
            """,
            (landing["quarantine_row_id"],),
        ).fetchone()
    return {
        "source_key": skey,
        "badge": BADGE if landing else "",
        "collision_count": len(collisions),
        "landing": _public(landing),
        "landing_row": dict(landing_row) if landing_row else None,
        "fingerprint": fp,
        "fingerprint_short": short_fp(fp),
        "landing_fingerprint": landing["fingerprint"] if landing else None,
        "fingerprint_match": bool(landing) and fp == landing["fingerprint"],
        "collision_fingerprints": [short_fp(c["fingerprint"]) for c in collisions],
    }


def item_status(conn, item_id: int) -> dict | None:
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
        return None
    data = status_for(conn, item["source_name"], item["item_key"], item["payload"])
    data["item_id"] = item["id"]
    data["batch_id"] = item["batch_id"]
    data["item_key"] = item["item_key"]
    data["quality"] = item["quality"]
    data["item_status"] = item["status"]
    return data


def status_for_item(item_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        return item_status(conn, item_id)


def assert_integrity(conn, skey: str):
    """徽标、撞件次数、去重落点必须互相咬合；拧花即拒重入舱。"""
    landing = _landing(conn, skey)
    collisions = _collisions(conn, skey)
    # 账册外已有同源件的隔离行，却没有落点：落点被抹掉，按失格闭死，绝不二次入舱。
    orphan = conn.execute(
        """
        SELECT r.id
        FROM quarantine_rows r
        JOIN capture_items i ON i.id=r.item_id
        JOIN capture_batches b ON b.id=i.batch_id
        WHERE trim(b.source_name) || '::' || trim(i.item_key) = ?
        LIMIT 1
        """,
        (skey,),
    ).fetchone()
    if landing is None and orphan is not None:
        raise ServiceError("去重落点缺失，拒绝二次入舱", 409)
    if landing:
        rid = landing["quarantine_row_id"]
        row = (
            conn.execute("SELECT id FROM quarantine_rows WHERE id=?", (rid,)).fetchone()
            if rid
            else None
        )
        if row is None:
            raise ServiceError("去重徽标与落点对不上：落点行缺失，拒绝重入舱", 409)
        if any(c["collision_of"] != landing["id"] for c in collisions):
            raise ServiceError("去重徽标与撞件次数对不上，拒绝重入舱", 409)
    elif collisions:
        raise ServiceError("去重徽标与撞件次数对不上：撞件记录无落点，拒绝重入舱", 409)
    return landing


def record_landing(conn, item, quarantine_row_id: int) -> dict:
    skey = source_key(item["source_name"], item["item_key"])
    cur = conn.execute(
        """
        INSERT INTO dedupe_ledger(
          source_key, capture_item_id, quarantine_row_id, fingerprint, fate, collision_of
        ) VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (skey, item["id"], quarantine_row_id, fingerprint(item["payload"]), LANDED),
    )
    return dict(conn.execute("SELECT * FROM dedupe_ledger WHERE id=?", (cur.lastrowid,)).fetchone())


def record_collision(conn, item, landing_row) -> dict:
    skey = source_key(item["source_name"], item["item_key"])
    cur = conn.execute(
        """
        INSERT INTO dedupe_ledger(
          source_key, capture_item_id, quarantine_row_id, fingerprint, fate, collision_of
        ) VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (skey, item["id"], fingerprint(item["payload"]), REFUSED, landing_row["id"]),
    )
    return dict(conn.execute("SELECT * FROM dedupe_ledger WHERE id=?", (cur.lastrowid,)).fetchone())


def refuse_duplicate(conn, item, landing_row) -> DedupeRefused:
    """在入舱事务内落一笔拒重，然后交回可抛出的白话拒绝。"""
    collision = record_collision(conn, item, landing_row)
    collisions = _collisions(conn, source_key(item["source_name"], item["item_key"]))
    row = (
        conn.execute(
            """
            SELECT r.id, b.bin_code
            FROM quarantine_rows r JOIN quarantine_bins b ON b.id=r.bin_id
            WHERE r.id=?
            """,
            (landing_row["quarantine_row_id"],),
        ).fetchone()
        if landing_row["quarantine_row_id"]
        else None
    )
    where = f"{row['bin_code']} 舱行 #{row['id']}" if row else f"账册落点 #{landing_row['id']}"
    message = (
        f"同一源件【{item['item_key']}】已入舱，落点 {where}；"
        f"本次为重复抓取，拒绝二次入舱（累计撞件 {len(collisions)} 次）"
    )
    return DedupeRefused(message, landing_row["id"], len(collisions))


def verify_retry(conn, item_id: int) -> dict:
    """回炉重试联验：徽标、落点行、抓取清单指纹必须对得上。"""
    status = item_status(conn, item_id)
    if not status or not status["badge"]:
        raise ServiceError("该件无去重落点，拒绝执行回炉", 409)
    if not status["landing_row"]:
        raise ServiceError("去重落点行缺失，拒绝执行回炉", 409)
    if not status["fingerprint_match"]:
        raise ServiceError("抓取清单指纹与去重账册对不上，拒绝执行回炉", 409)
    return status


def list_rows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.*, i.item_key, i.batch_id, bs.source_name,
                   r.status AS row_status, bn.bin_code
            FROM dedupe_ledger d
            JOIN capture_items i ON i.id=d.capture_item_id
            JOIN capture_batches bs ON bs.id=i.batch_id
            LEFT JOIN quarantine_rows r ON r.id=d.quarantine_row_id
            LEFT JOIN quarantine_bins bn ON bn.id=r.bin_id
            ORDER BY d.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            data = dict(r)
            data["fingerprint_short"] = short_fp(data["fingerprint"])
            data["badge"] = BADGE if data["fate"] == LANDED else "拒重入舱"
            out.append(data)
        return out


def stats(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        landings = conn.execute(
            "SELECT COUNT(*) AS c FROM dedupe_ledger WHERE fate=?", (LANDED,)
        ).fetchone()["c"]
        collisions = conn.execute(
            "SELECT COUNT(*) AS c FROM dedupe_ledger WHERE fate=?", (REFUSED,)
        ).fetchone()["c"]
    return {"landing_count": landings, "collision_count": collisions}


def dashboard(db_path: str | None = None) -> dict:
    rows = list_rows(db_path)
    with get_db(db_path) as conn:
        collisions = conn.execute(
            "SELECT COUNT(*) AS c FROM dedupe_ledger WHERE fate=?", (REFUSED,)
        ).fetchone()["c"]
    return {
        "title": TITLE,
        "label": TITLE,
        "form": FORM,
        "form_label": FORM_LABEL,
        "read_only": True,
        "policy": policy(),
        "rows": rows,
        "landing_count": len(rows) - collisions,
        "collision_count": collisions,
    }

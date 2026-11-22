from __future__ import annotations

import threading

import httpx
import pytest

from app.db import get_db
from app.services import captures, dedupe_ledger, quarantine, rework, settings
from app.services.errors import ServiceError


def _open_bin(db_path):
    return next(b for b in quarantine.list_bins(db_path) if b["status"] == "开放")


def _seed_item(db_path, item_key="sku-1002"):
    for batch in captures.list_batches(db_path):
        if batch["source_name"] != "站内目录甲" or batch["status"] == "已关闭":
            continue
        detail = captures.get_batch(batch["id"], db_path)
        found = next((i for i in detail["items"] if i["item_key"] == item_key), None)
        if found:
            return batch, found
    raise AssertionError(f"seed item {item_key} not found")


def _duplicate_item(db_path, item_key="sku-1002", payload='{"title":"重复抓取"}', quality="缺字段"):
    batch = captures.create_batch("站内目录甲", "重复抓取批", db_path)
    return batch, captures.add_item(batch["id"], item_key, payload, quality, db_path)


def _ledger(db_path):
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM dedupe_ledger ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def test_duplicate_refetch_refused_and_landing_recorded(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    landed = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)

    dup_batch, dup = _duplicate_item(db_path)
    with pytest.raises(ServiceError) as exc:
        quarantine.enter_item(dup["id"], bin_row["id"], "重复抓取", db_path)
    assert "已入舱" in exc.value.message or "拒绝" in exc.value.message
    assert exc.value.status_code == 409

    status = dedupe_ledger.status_for_item(dup["id"], db_path)
    assert status["badge"] == "已去重"
    assert status["collision_count"] == 1
    assert status["landing"]["capture_item_id"] == item["id"]
    assert status["landing"]["quarantine_row_id"] == landed["id"]
    assert status["landing_row"]["id"] == landed["id"]

    skey = dedupe_ledger.source_key("站内目录甲", "sku-1002")
    rows = [r for r in _ledger(db_path) if r["source_key"] == skey]
    landings = [r for r in rows if r["fate"] == "已落舱"]
    collisions = [r for r in rows if r["fate"] == "拒重"]
    assert len(landings) == 1
    assert len(collisions) == 1
    assert collisions[0]["source_key"] == landings[0]["source_key"]
    # 同源件换了内容再抓：撞件指纹不等于落点指纹，拒绝仍成立。
    assert collisions[0]["fingerprint"] != landings[0]["fingerprint"]
    assert collisions[0]["collision_of"] == landings[0]["id"]

    with get_db(db_path) as conn:
        dup_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id=?", (dup["id"],)
        ).fetchone()["c"]
    assert dup_rows == 0


def test_capture_side_reports_collision_count_and_badge(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    dup_batch, dup = _duplicate_item(db_path)
    with pytest.raises(ServiceError):
        quarantine.enter_item(dup["id"], bin_row["id"], "重复抓取", db_path)

    batches = captures.list_batches(db_path)
    listed = next(b for b in batches if b["id"] == dup_batch["id"])
    assert listed["collision_count"] == 1

    detail = captures.get_batch(dup_batch["id"], db_path)
    dup_row = next(i for i in detail["items"] if i["id"] == dup["id"])
    assert dup_row["dedupe_badge"] == "已去重"
    assert dup_row["collision_count"] == 1
    assert dup_row["dedupe_landing"]["quarantine_row_id"] is not None


def test_failed_entry_leaves_no_partial_ledger(db_path):
    _batch, _item = _seed_item(db_path)
    batch, item = _duplicate_item(db_path, item_key="sku-4002", payload="", quality="编码空")
    bin_row = _open_bin(db_path)
    with get_db(db_path) as conn:
        conn.execute("UPDATE quarantine_bins SET capacity=0 WHERE id=?", (bin_row["id"],))
    with pytest.raises(ServiceError):
        quarantine.enter_item(item["id"], bin_row["id"], "容量不足", db_path)
    skey = dedupe_ledger.source_key("站内目录甲", "sku-4002")
    assert [r for r in _ledger(db_path) if r["source_key"] == skey] == []


def test_concurrent_refetch_only_one_lands(db_path):
    bin_row = _open_bin(db_path)
    _b1, a = _duplicate_item(db_path, item_key="sku-9001", payload='{"n":1}', quality="缺字段")
    _b2, b = _duplicate_item(db_path, item_key="sku-9001", payload='{"n":2}', quality="缺字段")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def go(item_id: int) -> None:
        barrier.wait()
        try:
            quarantine.enter_item(item_id, bin_row["id"], "并发抢入", db_path)
            results.append("ok")
        except ServiceError:
            results.append("refused")

    threads = [threading.Thread(target=go, args=(a["id"],)), threading.Thread(target=go, args=(b["id"],))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1
    assert results.count("refused") == 1
    skey = dedupe_ledger.source_key("站内目录甲", "sku-9001")
    landings = [r for r in _ledger(db_path) if r["fate"] == "已落舱" and r["source_key"] == skey]
    collisions = [r for r in _ledger(db_path) if r["fate"] == "拒重" and r["source_key"] == skey]
    assert len(landings) == 1
    assert len(collisions) == 1
    total_rows = 0
    with get_db(db_path) as conn:
        total_rows = conn.execute("SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id IN (?,?)", (a["id"], b["id"])).fetchone()["c"]
    assert total_rows == 1


def test_rework_join_verifies_manifest_fingerprint(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    job = rework.enqueue_from_row(row["id"], db_path)

    jobs = rework.list_jobs(db_path)
    listed = next(j for j in jobs if j["id"] == job["id"])
    assert listed["dedupe_badge"] == "已去重"
    assert listed["collision_count"] == 0
    assert listed["dedupe_landing"]["quarantine_row_id"] == row["id"]
    assert listed["fingerprint_match"] is True

    with get_db(db_path) as conn:
        conn.execute("UPDATE capture_items SET payload='{\"title\":\"被篡改\"}' WHERE id=?", (item["id"],))
    with pytest.raises(ServiceError) as exc:
        rework.run_job(job["id"], succeed=True, db_path=db_path)
    assert "指纹" in exc.value.message
    with get_db(db_path) as conn:
        status = conn.execute("SELECT status FROM rework_jobs WHERE id=?", (job["id"],)).fetchone()["status"]
    assert status == "排队"


def test_rework_refuses_badge_without_landing_row(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    job = rework.enqueue_from_row(row["id"], db_path)
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE dedupe_ledger SET quarantine_row_id=NULL WHERE fate='已落舱' AND source_key=?",
            (dedupe_ledger.source_key("站内目录甲", item["item_key"]),),
        )
    with pytest.raises(ServiceError) as exc:
        rework.run_job(job["id"], succeed=True, db_path=db_path)
    assert "落点" in exc.value.message


def test_rework_refuses_when_no_ledger_at_all(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    job = rework.enqueue_from_row(row["id"], db_path)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM dedupe_ledger")
    with pytest.raises(ServiceError) as exc:
        rework.run_job(job["id"], succeed=True, db_path=db_path)
    assert "落点" in exc.value.message


def test_entry_refuses_when_landing_row_is_wiped(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    landed = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    dup_batch, dup = _duplicate_item(db_path)
    with get_db(db_path) as conn:
        conn.execute("UPDATE dedupe_ledger SET quarantine_row_id=NULL WHERE fate='已落舱' AND source_key=?",
                     (dedupe_ledger.source_key("站内目录甲", "sku-1002"),))
    with pytest.raises(ServiceError) as exc:
        quarantine.enter_item(dup["id"], bin_row["id"], "重复抓取", db_path)
    assert "落点" in exc.value.message
    with get_db(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id=?", (dup["id"],)).fetchone()["c"]
    assert count == 0
    assert landed["id"]  # 原落点行仍在，未被二次入舱覆盖


def test_entry_refuses_second_row_when_ledger_deleted(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    dup_batch, dup = _duplicate_item(db_path)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM dedupe_ledger")
    with pytest.raises(ServiceError) as exc:
        quarantine.enter_item(dup["id"], bin_row["id"], "重复抓取", db_path)
    assert exc.value.status_code == 409
    with get_db(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id=?", (dup["id"],)).fetchone()["c"]
    assert count == 0


def test_feature_absent_badge_only_row_is_failure(db_path):
    """仅有去重徽标、没有落点行：回炉联验必须拒绝。"""
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    job = rework.enqueue_from_row(row["id"], db_path)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM dedupe_ledger")
    jobs = rework.list_jobs(db_path)
    listed = next(j for j in jobs if j["id"] == job["id"])
    assert listed["dedupe_badge"] == ""
    assert listed["landing_row"] is None
    with pytest.raises(ServiceError):
        rework.run_job(job["id"], succeed=True, db_path=db_path)


def test_two_path_race_no_double_quarantine_row(db_path):
    bin_row = _open_bin(db_path)
    _b1, a = _duplicate_item(db_path, item_key="sku-9101", payload='{"n":"a"}', quality="缺字段")
    _b2, b = _duplicate_item(db_path, item_key="sku-9101", payload='{"n":"b"}', quality="缺字段")
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def go(item_id: int) -> None:
        barrier.wait()
        try:
            quarantine.enter_item(item_id, bin_row["id"], "抢入", db_path)
            ok = True
        except ServiceError:
            ok = False
        with lock:
            outcomes.append(ok)

    threads = [threading.Thread(target=go, args=(a["id"],)), threading.Thread(target=go, args=(b["id"],))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [False, True]
    skey = dedupe_ledger.source_key("站内目录甲", "sku-9101")
    rows = [r for r in _ledger(db_path) if r["source_key"] == skey]
    assert len([r for r in rows if r["fate"] == "已落舱"]) == 1
    assert len([r for r in rows if r["fate"] == "拒重"]) == 1
    with get_db(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id IN (?,?)", (a["id"], b["id"])
        ).fetchone()["c"]
    assert n == 1


def test_bins_expose_fingerprint_strip(db_path):
    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    bins = quarantine.list_bins(db_path)
    listed = next(b for b in bins if b["id"] == bin_row["id"])
    assert listed["dedupe_landing_count"] == len(listed["fingerprints"]) >= 1
    assert row["id"] in [f["quarantine_row_id"] for f in listed["fingerprints"]]
    assert all(f["fingerprint_short"] for f in listed["fingerprints"])


def test_settings_dedupe_policy_is_read_only(db_path):
    policy = settings.dedupe_policy(db_path)
    assert policy["form"] == "ledger"
    assert policy["form_label"] == "独立账册"
    assert policy["read_only"] is True
    assert policy["fingerprint_ref"] == "dedupe_ledger"


def test_dedupe_dashboard_reports_single_form(db_path):
    dash = dedupe_ledger.dashboard(db_path)
    assert dash["form"] == "ledger"
    assert dash["read_only"] is True
    assert dash["form"] in ("ledger",)
    assert "cutover" not in dash
    assert "dualwrite" not in dash
    assert dash["read_only"] is True


def test_policy_apply_counts_refusal_instead_of_double_entering(db_path):
    from app.services import policies

    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    dup_batch, dup = _duplicate_item(db_path)
    result = policies.apply_to_batch(dup_batch["id"], db_path)
    assert result["entered"] == 0
    assert result["refused"] == 1
    with get_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id=?", (dup["id"],)).fetchone()["c"]
    assert n == 0


def test_review_release_refuses_when_fingerprint_drifted(db_path):
    from app.services import review

    _batch, item = _seed_item(db_path)
    bin_row = _open_bin(db_path)
    row = quarantine.enter_item(item["id"], bin_row["id"], "首轮坏件", db_path)
    ticket = next(t for t in review.list_tickets(db_path) if t["row_id"] == row["id"])
    with get_db(db_path) as conn:
        conn.execute("UPDATE capture_items SET payload='{\"title\":\"换过\"}' WHERE id=?", (item["id"],))
    with pytest.raises(ServiceError) as exc:
        review.decide(ticket["id"], "放行", "tester", "放行", db_path)
    assert "指纹" in exc.value.message
    with get_db(db_path) as conn:
        state = conn.execute("SELECT status FROM quarantine_rows WHERE id=?", (row["id"],)).fetchone()["status"]
    assert state == "待复核"


def test_http_surface_and_removed_scaffolding(db_path):
    from app.main import create_app

    app = create_app()
    transport = httpx.WSGITransport(app=app)
    with httpx.Client(transport=transport, base_url="http://testserver") as client:
        login = client.post("/api/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200
        dash = client.get("/api/dedupe/dash")
        assert dash.status_code == 200
        body = dash.json()
        assert body["form"] == "ledger"
        assert body["policy"]["read_only"] is True
        settings_resp = client.get("/api/settings").json()
        assert settings_resp["dedupe_policy"]["read_only"] is True
        assert "items" in settings_resp
        for path in ("/api/dedupe_fp/half", "/api/dedupe_fp/cutover", "/api/dedupe_fp/dual", "/api/dedupe_fp/dash"):
            assert client.post(path, json={}).status_code == 404

        first = client.get("/api/batches").json()["items"][0]
        detail = client.get(f"/api/batches/{first['id']}").json()
        assert "collision_count" in detail
        assert all("dedupe_badge" in i for i in detail["items"])

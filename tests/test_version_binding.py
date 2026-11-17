import threading

from app.db import get_db


def _publish(client, domain_code, keep_days, action, note=""):
    r = client.post(
        "/api/policies/publish",
        json={
            "domain_code": domain_code,
            "keep_days": keep_days,
            "action_on_expire": action,
            "change_note": note,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_scan_binds_snapshot_and_exposes_live_version(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert items
    for j in items:
        assert j["policy_version"] == j["live_version"]
        assert j["binding_state"] == "一致"
        assert j["snapshot_at"]
        assert j["disposition"] == "绑定快照"


def test_keep_days_shortening_marks_old_judgment_stale(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    before = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(j for j in before if j["keep_days"] > 3)

    # 缩天：保留天从 7 天改成 3 天，旧裁决快照必须保持不变
    _publish(authed, target["domain_code"], 3, target["decided_action"], "缩天 7→3")

    after = authed.get("/api/judgments", params={"binding_state": "stale"}).json()["items"]
    stale_ids = {j["id"] for j in after}
    assert target["id"] in stale_ids

    refreshed = next(
        j
        for j in authed.get("/api/judgments").json()["items"]
        if j["id"] == target["id"]
    )
    # 绑定快照未被换版改写；在用版独立变化
    assert refreshed["keep_days"] == target["keep_days"]
    assert refreshed["policy_version"] == target["policy_version"]
    assert refreshed["live_version"] == target["policy_version"] + 1
    assert refreshed["live_keep_days"] == 3
    assert refreshed["binding_state"] == "缩天待重扫"

    # 未重扫前执行必须拒绝，不能按旧口径落库
    r = authed.post(f"/api/judgments/{target['id']}/execute")
    assert r.status_code == 409
    assert "重扫" in r.json()["detail"]

    # 重扫：旧单作废，新单绑定新版本
    scan = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"}).json()["job"]
    assert scan["revoked_count"] >= 1
    assert scan["rescanned_count"] >= 1
    assert "换版" in scan["message"]

    all_items = authed.get("/api/judgments").json()["items"]
    old = next(j for j in all_items if j["id"] == target["id"])
    assert old["status"] == "已作废"
    assert old["disposition"] == "重扫作废"
    assert old["superseded_by_id"]
    new = next(j for j in all_items if j["id"] == old["superseded_by_id"])
    assert new["status"] == "待执行"
    assert new["policy_version"] == old["policy_version"] + 1
    assert new["keep_days"] == 3
    assert new["disposition"] == "重扫重开"

    # 重扫后可以正常执行
    ok = authed.post(f"/api/judgments/{new['id']}/execute")
    assert ok.status_code == 200
    assert ok.json()["run"]["ok"] == 1


def test_keep_days_lengthening_revokes_without_reopen(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    # 挑一个分区：把保留天拉长到远超其龄期，重扫后不应重开
    target = next(
        j
        for j in items
        if j["domain_code"] == "events" and j["biz_date"] == "2026-11-08"
    )
    _publish(authed, "events", 400, "销毁", "延长期限")

    scan = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"}).json()["job"]
    detail = [
        d
        for d in authed.get(f"/api/scan-jobs/{scan['id']}").json()["job"]["detail"]
        if d["partition_id"] == target["partition_id"]
    ]
    assert detail and detail[0]["outcome"] == "重扫作废不重开"

    row = next(
        j for j in authed.get("/api/judgments").json()["items"] if j["id"] == target["id"]
    )
    assert row["status"] == "已作废"
    assert row["superseded_by_id"] is None


def test_judgment_detail_and_scan_summary_are_consistent(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    _publish(authed, "orders", 1, "归档", "缩天")
    scan = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"}).json()["job"]
    detail = authed.get(f"/api/scan-jobs/{scan['id']}").json()["job"]["detail"]

    revoked = [d for d in detail if d["outcome"] == "重扫重开"]
    fresh = [d for d in detail if d["outcome"] == "新开单"]
    assert len(revoked) == scan["rescanned_count"]
    # 扫描摘要的开单数与逐分区明细的处置条数同源，不是各写各的。
    assert scan["opened_count"] == len(revoked) + len(fresh)
    assert scan["revoked_count"] == sum(
        1 for d in detail if d["outcome"] in ("重扫重开", "重扫作废不重开")
    )
    assert len(revoked) >= 1

    # 队列摘要口径与裁决落库口径一致：开放裁决里无一条落后在用版
    stale = authed.get("/api/judgments", params={"binding_state": "stale"}).json()["items"]
    assert [j for j in stale if j["status"] in ("待执行", "执行中")] == []


def test_scan_job_slot_rejects_concurrent_scan(authed):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_jobs (as_of_date, status, message) VALUES (?,?,?)",
            ("2026-11-16", "进行中", "占坑中"),
        )
    r = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert r.status_code == 409
    assert "扫描" in r.json()["detail"]


def test_two_concurrent_scans_do_not_double_open(authed):
    """两名试跑同时扫描：占坑锁写串行化，不产生同分区双开、互盖。"""
    from app.config import Config
    from app.services.scan import run_expire_scan

    results = []
    errors = []

    def worker():
        try:
            with get_db(Config.database()) as conn:
                results.append(run_expire_scan(conn, "2026-11-16"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 2
    assert sum(r["opened_count"] for r in results) >= 1

    with get_db() as conn:
        dups = conn.execute(
            "SELECT partition_id, COUNT(*) AS c FROM judgments "
            "WHERE status IN ('待执行','执行中') GROUP BY partition_id "
            "HAVING c > 1"
        ).fetchall()
    assert dups == []


def test_execute_rejects_concurrent_second_claim(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    first = pending[0]
    assert authed.post(f"/api/judgments/{first['id']}/execute").status_code == 200
    second = authed.post(f"/api/judgments/{first['id']}/execute")
    assert second.status_code == 409


def test_publish_requires_actual_change(authed):
    r = authed.post(
        "/api/policies/publish",
        json={"domain_code": "orders", "keep_days": 7, "action_on_expire": "归档"},
    )
    assert r.status_code == 400
    assert "无需换版" in r.json()["detail"]


def test_execute_failure_is_recorded_and_retryable(authed):
    """执行异常必须落「已失败 + 失败原因 + 分区恢复在线」，不能被事务回滚抹掉。"""
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = pending[0]
    with get_db() as conn:
        conn.execute(
            "UPDATE judgments SET decided_action = '未知动作' WHERE id = ?",
            (target["id"],),
        )

    r = authed.post(f"/api/judgments/{target['id']}/execute")
    assert r.status_code == 409
    assert "未知动作" in r.json()["detail"]

    items = authed.get("/api/judgments").json()["items"]
    row = next(j for j in items if j["id"] == target["id"])
    assert row["status"] == "已失败"
    assert "未知动作" in row["fail_reason"]
    runs = authed.get("/api/runs").json()["items"]
    failed = next(x for x in runs if x["judgment_id"] == target["id"])
    assert failed["ok"] == 0
    parts = authed.get("/api/partitions").json()["items"]
    part = next(p for p in parts if p["id"] == target["partition_id"])
    assert part["storage_state"] == "在线"

    # 修复动作后可重试
    with get_db() as conn:
        conn.execute(
            "UPDATE judgments SET decided_action = ? WHERE id = ?",
            (target["decided_action"], target["id"]),
        )
    assert authed.post(f"/api/judgments/{target['id']}/execute").status_code == 200


def test_completed_judgment_is_closed_even_after_policy_change(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(j for j in pending if j["domain_code"] == "orders")
    authed.post(f"/api/judgments/{target['id']}/execute")

    _publish(authed, "orders", 30, "销毁", "换版")
    row = next(
        j
        for j in authed.get("/api/judgments").json()["items"]
        if j["id"] == target["id"]
    )
    assert row["status"] == "已完成"
    assert row["binding_state"] == "已收口"
    # 已完成的历史裁决按旧快照留痕，不被换版改写成新动作
    assert row["decided_action"] == target["decided_action"]
    assert row["policy_version"] == target["policy_version"]

    stale = authed.get("/api/judgments", params={"binding_state": "stale"}).json()["items"]
    assert all(j["status"] in ("待执行", "执行中", "已失败") for j in stale)

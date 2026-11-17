import json
from contextlib import contextmanager
from datetime import date, datetime

import sqlite3

from app.services.policies import get_active_policy

OPEN_STATUSES = ("待执行", "执行中", "已失败")


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def write_lock(conn: sqlite3.Connection):
    """扫描的「读在用版 → 判到期 → 开单」整段拿库级写锁，两名试跑互不覆盖。"""
    started = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        started = True
    try:
        yield
    except Exception:
        if started:
            conn.rollback()
        raise
    else:
        if started:
            conn.commit()


def _open_judgment(
    conn: sqlite3.Connection,
    part: sqlite3.Row,
    policy: dict,
    job_id: int,
    disposition: str,
    note: str,
    supersedes_id: int | None = None,
) -> int:
    """开单时一次性抄下策略快照：后续换版不再改写这条裁决。"""
    cur = conn.execute(
        "INSERT INTO judgments "
        "(scan_job_id, partition_id, domain_code, biz_date, policy_version, "
        "keep_days, decided_action, status, disposition, note, snapshot_at, "
        "supersedes_id) "
        "VALUES (?,?,?,?,?,?,?, '待执行', ?,?,?,?)",
        (
            job_id,
            part["id"],
            part["domain_code"],
            part["biz_date"],
            int(policy["version"]),
            int(policy["keep_days"]),
            policy["action_on_expire"],
            disposition,
            note,
            _today_stamp(),
            supersedes_id,
        ),
    )
    return int(cur.lastrowid)


def _detail_row(
    part: sqlite3.Row,
    age: int,
    live: dict,
    bound_version: int,
    bound_keep_days: int,
    outcome: str,
    **extra: object,
) -> dict:
    row = {
        "partition_id": part["id"],
        "domain_code": part["domain_code"],
        "biz_date": part["biz_date"],
        "age_days": age,
        "live_version": int(live["version"]),
        "live_keep_days": int(live["keep_days"]),
        "live_action": live["action_on_expire"],
        "bound_version": bound_version,
        "bound_keep_days": bound_keep_days,
        "outcome": outcome,
    }
    row.update(extra)
    return row


def run_expire_scan(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """扫描在线分区：按「扫描时的在用版」开单，并把裁决绑定到该版快照。

    换版处置（本实验室口径：重扫，不锁旧）：
      - 开放裁决绑定的策略版本 != 当前在用版 → 一律重扫；
      - 新在用版下仍到期 → 作废旧单并按新快照重开，写 supersedes 链；
      - 新在用版下已不到期（延期）→ 只作废、不重开。
    并发：库级 BEGIN IMMEDIATE 串行化「读-判-写」；
    `scan_jobs`「进行中」与 `judgments` 同分区开放裁决各有部分唯一索引兜底，
    两名试跑同时扫描不会双写互盖。
    """
    as_of = _parse_day(as_of_date)
    with write_lock(conn):
        if conn.execute(
            "SELECT id FROM scan_jobs WHERE status = '进行中' LIMIT 1"
        ).fetchone():
            raise ValueError("已有到期扫描在执行中，请稍后重试")

        cur = conn.execute(
            "INSERT INTO scan_jobs (as_of_date, status, message) VALUES (?,?,?)",
            (as_of_date, "进行中", ""),
        )
        job_id = int(cur.lastrowid)

        online = conn.execute(
            "SELECT id, domain_code, biz_date, storage_state FROM partitions "
            "WHERE storage_state = '在线' ORDER BY domain_code, biz_date"
        ).fetchall()

        expired = opened = rescanned = revoked = skipped_open = 0
        detail: list[dict] = []
        summary_bits: dict[str, dict] = {}

        for part in online:
            live = get_active_policy(conn, part["domain_code"])
            if not live:
                continue
            live_version = int(live["version"])
            age = (as_of - _parse_day(part["biz_date"])).days
            expired_live = age >= int(live["keep_days"])
            if expired_live:
                expired += 1

            open_rows = conn.execute(
                "SELECT id, policy_version, keep_days, decided_action, status "
                "FROM judgments WHERE partition_id = ? "
                "AND status IN ('待执行','执行中','已失败') "
                "ORDER BY id DESC",
                (part["id"],),
            ).fetchall()

            stale = [r for r in open_rows if int(r["policy_version"]) != live_version]
            live_open = [
                r for r in open_rows if int(r["policy_version"]) == live_version
            ]
            reopened_here = False
            live_note = (
                "已有在用版开放单，作废旧绑定后不重复开单"
                if live_open
                else "同分区已按新快照重开"
            )

            # 1) 换版后按「重扫」处置旧绑定的裁决
            for old in stale:
                if old["status"] == "执行中":
                    # 执行中的单子不半途改写；留给执行侧收口。
                    skipped_open += 1
                    detail.append(
                        _detail_row(
                            part, age, live,
                            int(old["policy_version"]), int(old["keep_days"]),
                            "执行中保留",
                            judgment_id=int(old["id"]),
                        )
                    )
                    continue

                conn.execute(
                    "UPDATE judgments SET status = '已作废', disposition = '重扫作废', "
                    "note = ? WHERE id = ?",
                    (
                        f"策略换版 v{old['policy_version']}→v{live_version}，"
                        "旧绑定裁决作废待重扫",
                        old["id"],
                    ),
                )
                revoked += 1
                new_id = None
                if expired_live and not (live_open or reopened_here):
                    # 同一分区只重开一次：历史遗留的多条旧绑定一并作废，
                    # 新快照的待执行单只有一张。
                    new_id = _open_judgment(
                        conn,
                        part,
                        live,
                        job_id,
                        disposition="重扫重开",
                        note=f"重扫取代 #{old['id']}（旧绑 v{old['policy_version']}）",
                        supersedes_id=int(old["id"]),
                    )
                    conn.execute(
                        "UPDATE judgments SET superseded_by_id = ? WHERE id = ?",
                        (new_id, old["id"]),
                    )
                    rescanned += 1
                    opened += 1
                    reopened_here = True
                    outcome = "重扫重开"
                elif expired_live:
                    outcome = f"重扫作废（{live_note}）"
                else:
                    outcome = "重扫作废不重开"

                detail.append(
                    _detail_row(
                        part, age, live,
                        int(old["policy_version"]), int(old["keep_days"]),
                        outcome,
                        revoked_judgment_id=int(old["id"]),
                        judgment_id=new_id,
                    )
                )
                bit = summary_bits.setdefault(
                    f"{part['domain_code']} v{old['policy_version']}→v{live_version}",
                    {"revoked": 0, "reopened": 0},
                )
                bit["revoked"] += 1
                if new_id is not None:
                    bit["reopened"] += 1

            if reopened_here:
                continue

            # 2) 已有绑定在用版的开放裁决 → 不重复开单
            if live_open:
                skipped_open += len(live_open)
                if expired_live:
                    detail.append(
                        _detail_row(
                            part, age, live,
                            live_version, int(live_open[0]["keep_days"]),
                            "占用跳过",
                            judgment_id=int(live_open[0]["id"]),
                        )
                    )
                continue

            # 3) 无开放裁决且按在用版到期 → 新开单并绑定快照
            if not expired_live:
                continue
            new_id = _open_judgment(
                conn,
                part,
                live,
                job_id,
                disposition="绑定快照",
                note=f"按在用版 v{live_version} 到期开单（{age}天 ≥ {live['keep_days']}天）",
            )
            opened += 1
            detail.append(
                _detail_row(
                    part, age, live,
                    live_version, int(live["keep_days"]),
                    "新开单",
                    judgment_id=new_id,
                )
            )

        binding_summary = "；".join(
            f"{key} 作废{b['revoked']}/重开{b['reopened']}"
            for key, b in sorted(summary_bits.items())
        )
        message = (
            f"到期 {expired} 条；开单 {opened} 条"
            f"（重扫重开 {rescanned}）；作废 {revoked} 条；占用跳过 {skipped_open} 条"
        )
        if binding_summary:
            message += f"；换版 {binding_summary}"

        conn.execute(
            "UPDATE scan_jobs SET status = ?, expired_count = ?, opened_count = ?, "
            "rescanned_count = ?, revoked_count = ?, skipped_open_count = ?, "
            "binding_summary = ?, detail_json = ?, message = ? WHERE id = ?",
            (
                "已完成",
                expired,
                opened,
                rescanned,
                revoked,
                skipped_open,
                binding_summary,
                json.dumps(detail, ensure_ascii=False),
                message,
                job_id,
            ),
        )
    return get_scan_job(conn, job_id)


def get_scan_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, as_of_date, status, expired_count, opened_count, "
        "rescanned_count, revoked_count, skipped_open_count, binding_summary, "
        "detail_json, message, created_at FROM scan_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    job = dict(row)
    job["detail"] = json.loads(job.pop("detail_json") or "[]")
    return job


def list_scan_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, status, expired_count, opened_count, "
        "rescanned_count, revoked_count, skipped_open_count, binding_summary, "
        "message, created_at FROM scan_jobs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


JUDGMENT_SELECT = """
SELECT j.id, j.scan_job_id, j.partition_id, j.domain_code, j.biz_date,
       j.policy_version, j.keep_days, j.decided_action, j.status,
       j.disposition, j.note, j.snapshot_at, j.supersedes_id,
       j.superseded_by_id, j.fail_reason, j.created_at,
       live.version AS live_version,
       live.keep_days AS live_keep_days,
       live.action_on_expire AS live_action,
       CASE
         WHEN j.status NOT IN ('待执行','执行中','已失败') THEN '已收口'
         WHEN live.id IS NULL THEN '在用版缺失'
         WHEN j.policy_version = live.version THEN '一致'
         WHEN j.keep_days > live.keep_days THEN '缩天待重扫'
         WHEN j.keep_days < live.keep_days THEN '延期待重扫'
         ELSE '动作变更待重扫'
       END AS binding_state
FROM judgments j
LEFT JOIN retain_policies live
  ON live.domain_code = j.domain_code AND live.active = 1
"""


def list_judgments(
    conn: sqlite3.Connection,
    status: str | None = None,
    scan_job_id: int | None = None,
    binding_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("j.status = ?")
        params.append(status)
    if scan_job_id is not None:
        clauses.append("j.scan_job_id = ?")
        params.append(scan_job_id)
    if binding_state == "stale":
        clauses.append(
            "j.status IN ('待执行','执行中','已失败') "
            "AND live.id IS NOT NULL AND j.policy_version != live.version"
        )
    elif binding_state == "aligned":
        clauses.append(
            "j.status IN ('待执行','执行中','已失败') "
            "AND live.id IS NOT NULL AND j.policy_version = live.version"
        )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        JUDGMENT_SELECT + where + " ORDER BY j.id DESC", params
    ).fetchall()
    return [dict(r) for r in rows]

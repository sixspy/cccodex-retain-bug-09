import sqlite3

from app.services.partitions import get_partition
from app.services.policies import get_active_policy, get_policy_version


def _binding_note(judgment: dict, live: dict | None) -> str:
    bound = (
        f"绑定快照 v{judgment['policy_version']} / {judgment['keep_days']}天"
        f" / {judgment['decided_action']}"
    )
    if not live:
        return bound + "；在用版缺失"
    if int(judgment["policy_version"]) == int(live["version"]):
        return bound + f"；与在用版 v{live['version']} 一致"
    return (
        bound
        + f"；在用版已换为 v{live['version']} / {live['keep_days']}天，"
        "该单待重扫，不按新口径执行"
    )


def _claim(conn: sqlite3.Connection, judgment_id: int) -> tuple[dict, dict, dict]:
    row = conn.execute("SELECT * FROM judgments WHERE id = ?", (judgment_id,)).fetchone()
    if not row:
        raise ValueError("裁决不存在")
    judgment = dict(row)
    if judgment["status"] not in ("待执行", "已失败"):
        raise ValueError(f"当前状态不可执行：{judgment['status']}")

    live = get_active_policy(conn, judgment["domain_code"])
    if live and int(judgment["policy_version"]) != int(live["version"]):
        bound = get_policy_version(
            conn, judgment["domain_code"], int(judgment["policy_version"])
        )
        if bound:
            raise ValueError(
                f"裁决绑定策略 v{judgment['policy_version']}"
                f"（保留{judgment['keep_days']}天）已被在用版 v{live['version']}"
                f"（保留{live['keep_days']}天）取代；请先重跑到期扫描，"
                "由重扫作废并重开后再执行，避免按旧口径落库"
            )

    part = get_partition(conn, judgment["partition_id"])
    if not part:
        raise ValueError("分区不存在")
    if part["storage_state"] not in ("在线", "裁撤中"):
        raise ValueError(f"分区状态不可裁撤：{part['storage_state']}")

    # 条件更新认领：并发下只有一个请求能把单子推进到「执行中」。
    claimed = conn.execute(
        "UPDATE judgments SET status = '执行中', fail_reason = '' "
        "WHERE id = ? AND status IN ('待执行','已失败')",
        (judgment_id,),
    )
    if claimed.rowcount != 1:
        raise ValueError("裁决已被其他执行占用，请刷新后查看")
    conn.execute(
        "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
        (part["id"],),
    )
    return judgment, part, live or {}


def execute_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """执行单条裁决：归档写入 archive_uri；销毁清空在线态。整单失败则回滚动作并记失败。

    口径：
      - 执行严格按「开单绑定快照」的保留天与动作，不偷换现行版；
      - 绑定版已被现行版取代且未重扫时拒绝执行，提示先重跑扫描
        （本实验室选择「重扫」而非「锁旧」，不在此处静默按旧口径放行）；
      - 认领用条件更新，「执行中」不会被并发的扫描读到。
    """
    judgment, part, live = _claim(conn, judgment_id)
    action = judgment["decided_action"]

    conn.execute("SAVEPOINT execute_one")
    try:
        if action == "归档":
            uri = f"archive://{part['domain_code']}/{part['biz_date']}"
            conn.execute(
                "UPDATE partitions SET storage_state = '已归档', archive_uri = ? "
                "WHERE id = ?",
                (uri, part["id"]),
            )
            result_state = "已归档"
            archive_uri = uri
            detail = "分区已归档并保留可查归档地址"
        elif action == "销毁":
            conn.execute(
                "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                "row_count = 0 WHERE id = ?",
                (part["id"],),
            )
            result_state = "已销毁"
            archive_uri = None
            detail = "分区已销毁，在线行数清零"
        else:
            raise ValueError(f"未知动作：{action}")

        note = _binding_note(judgment, live)
        conn.execute(
            "UPDATE judgments SET status = '已完成', note = ? WHERE id = ?",
            (note, judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (judgment_id, action, result_state, archive_uri, f"{detail}；{note}"),
        )
        conn.execute("RELEASE execute_one")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — 失败闭：撤销动作、恢复在线、记失败原因
        conn.execute("ROLLBACK TO execute_one")
        conn.execute("RELEASE execute_one")
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL WHERE id = ?",
            (part["id"],),
        )
        conn.execute(
            "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
            (str(exc), judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, "在线", None, str(exc)),
        )
        # 失败记录要先落盘，否则外层事务回滚会把「已失败」一起抹掉。
        conn.commit()
        raise

    return get_run_by_judgment(conn, judgment_id)


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, r.judgment_id, r.action, r.result_state, r.archive_uri, "
        "r.ok, r.detail, r.created_at, j.policy_version, j.keep_days, "
        "j.domain_code, j.biz_date "
        "FROM execute_runs r JOIN judgments j ON j.id = r.judgment_id "
        "ORDER BY r.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]

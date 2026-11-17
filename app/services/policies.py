import sqlite3

POLICY_FIELDS = (
    "id, domain_code, domain_name, keep_days, action_on_expire, "
    "version, active, change_note, created_at"
)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def list_policies(conn: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = f"SELECT {POLICY_FIELDS} FROM retain_policies"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY domain_code, version DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_active_policy(conn: sqlite3.Connection, domain_code: str) -> dict | None:
    row = conn.execute(
        f"SELECT {POLICY_FIELDS} FROM retain_policies "
        "WHERE domain_code = ? AND active = 1 "
        "ORDER BY version DESC LIMIT 1",
        (domain_code,),
    ).fetchone()
    return _row_to_dict(row)


def get_policy_version(
    conn: sqlite3.Connection, domain_code: str, version: int
) -> dict | None:
    row = conn.execute(
        f"SELECT {POLICY_FIELDS} FROM retain_policies "
        "WHERE domain_code = ? AND version = ?",
        (domain_code, version),
    ).fetchone()
    return _row_to_dict(row)


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    """策略版本队列：同一主题域内按版本倒序，active=1 即当前在用版。"""
    return list_policies(conn, active_only=False)


def publish_version(
    conn: sqlite3.Connection,
    domain_code: str,
    keep_days: int,
    action_on_expire: str,
    change_note: str = "",
) -> dict:
    """发布新策略版本：旧版本全部下线，新版本置为在用版。

    换版动作本身不直接改写已开裁决——裁决继续绑定开单快照，
    由下一次到期扫描按「重扫作废并重开」收敛（见 services/scan.py）。
    """
    if action_on_expire not in ("归档", "销毁"):
        raise ValueError("到期动作只能是「归档」或「销毁」")
    if int(keep_days) <= 0:
        raise ValueError("保留天数必须为正整数")

    live = get_active_policy(conn, domain_code)
    if not live:
        raise ValueError(f"主题域不存在：{domain_code}")
    if int(keep_days) == int(live["keep_days"]) and action_on_expire == live["action_on_expire"]:
        raise ValueError("保留天与到期动作均未变化，无需换版")

    next_version = int(
        conn.execute(
            "SELECT MAX(version) AS v FROM retain_policies WHERE domain_code = ?",
            (domain_code,),
        ).fetchone()["v"]
    ) + 1
    conn.execute(
        "UPDATE retain_policies SET active = 0 WHERE domain_code = ?",
        (domain_code,),
    )
    conn.execute(
        "INSERT INTO retain_policies "
        "(domain_code, domain_name, keep_days, action_on_expire, version, active, change_note) "
        "VALUES (?,?,?,?,?,1,?)",
        (
            domain_code,
            live["domain_name"],
            int(keep_days),
            action_on_expire,
            next_version,
            change_note.strip() or "策略换版",
        ),
    )
    return {
        "previous": live,
        "current": get_policy_version(conn, domain_code, next_version),
        "direction": (
            "缩天" if int(keep_days) < int(live["keep_days"])
            else "延期" if int(keep_days) > int(live["keep_days"])
            else "动作变更"
        ),
        "stale_open_judgments": count_open_bound_to(
            conn, domain_code, int(live["version"])
        ),
    }


def count_open_bound_to(
    conn: sqlite3.Connection, domain_code: str, version: int
) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM judgments "
            "WHERE domain_code = ? AND policy_version = ? "
            "AND status IN ('待执行','执行中','已失败')",
            (domain_code, version),
        ).fetchone()["c"]
    )


def binding_board(conn: sqlite3.Connection) -> list[dict]:
    """策略队列的绑定核对板：每个主题域「在用版」对照「开单绑定版」。"""
    live_rows = conn.execute(
        f"SELECT {POLICY_FIELDS} FROM retain_policies WHERE active = 1 "
        "ORDER BY domain_code"
    ).fetchall()
    board: list[dict] = []
    for live in live_rows:
        bound = conn.execute(
            "SELECT policy_version, keep_days, decided_action, COUNT(*) AS c "
            "FROM judgments WHERE domain_code = ? "
            "AND status IN ('待执行','执行中','已失败') "
            "GROUP BY policy_version, keep_days, decided_action "
            "ORDER BY policy_version DESC",
            (live["domain_code"],),
        ).fetchall()
        bound_versions = [dict(r) for r in bound]
        stale = sum(
            b["c"] for b in bound_versions
            if int(b["policy_version"]) != int(live["version"])
        )
        board.append(
            {
                "domain_code": live["domain_code"],
                "domain_name": live["domain_name"],
                "live_version": live["version"],
                "live_keep_days": live["keep_days"],
                "live_action": live["action_on_expire"],
                "bound_versions": bound_versions,
                "open_total": sum(b["c"] for b in bound_versions),
                "stale_open": stale,
            }
        )
    return board

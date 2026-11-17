import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发扫描靠 SQLite 写锁串行化；等待而不是立刻报「database is locked」。
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS retain_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  domain_name TEXT NOT NULL,
  keep_days INTEGER NOT NULL,
  action_on_expire TEXT NOT NULL CHECK (action_on_expire IN ('归档','销毁')),
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  change_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, version)
);

CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  storage_state TEXT NOT NULL CHECK (
    storage_state IN ('在线','已归档','已销毁','裁撤中')
  ),
  archive_uri TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, biz_date)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL,
  expired_count INTEGER NOT NULL DEFAULT 0,
  opened_count INTEGER NOT NULL DEFAULT 0,
  rescanned_count INTEGER NOT NULL DEFAULT 0,
  revoked_count INTEGER NOT NULL DEFAULT 0,
  skipped_open_count INTEGER NOT NULL DEFAULT 0,
  binding_summary TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '[]',
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_job_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  keep_days INTEGER NOT NULL,
  decided_action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','已完成','已失败','已作废')),
  disposition TEXT NOT NULL DEFAULT '绑定快照',
  note TEXT NOT NULL DEFAULT '',
  snapshot_at TEXT NOT NULL DEFAULT '',
  supersedes_id INTEGER,
  superseded_by_id INTEGER,
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(scan_job_id) REFERENCES scan_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS execute_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id INTEGER NOT NULL UNIQUE,
  action TEXT NOT NULL,
  result_state TEXT NOT NULL,
  archive_uri TEXT,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  online_count INTEGER NOT NULL,
  archived_count INTEGER NOT NULL,
  destroyed_count INTEGER NOT NULL,
  pending_judgment_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


# 兼容已落盘的本机库：新列按需 ALTER TABLE 补上，避免老库直接 500。
JUDGMENT_COLUMNS = [
    ("disposition", "TEXT NOT NULL DEFAULT '绑定快照'"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
    ("snapshot_at", "TEXT NOT NULL DEFAULT ''"),
    ("supersedes_id", "INTEGER"),
    ("superseded_by_id", "INTEGER"),
]

SCAN_JOB_COLUMNS = [
    ("opened_count", "INTEGER NOT NULL DEFAULT 0"),
    ("rescanned_count", "INTEGER NOT NULL DEFAULT 0"),
    ("revoked_count", "INTEGER NOT NULL DEFAULT 0"),
    ("skipped_open_count", "INTEGER NOT NULL DEFAULT 0"),
    ("binding_summary", "TEXT NOT NULL DEFAULT ''"),
    ("detail_json", "TEXT NOT NULL DEFAULT '[]'"),
]

POLICY_COLUMNS = [
    ("change_note", "TEXT NOT NULL DEFAULT ''"),
]


SEED_POLICIES = [
    ("orders", "订单明细域", 7, "归档"),
    ("payments", "支付流水域", 14, "归档"),
    ("events", "埋点事件域", 3, "销毁"),
    ("inventory", "库存快照域", 10, "归档"),
]

SEED_PARTITIONS = [
    ("orders", "2026-11-01", 1200, "在线"),
    ("orders", "2026-11-05", 980, "在线"),
    ("orders", "2026-11-10", 1100, "在线"),
    ("orders", "2026-11-14", 1050, "在线"),
    ("payments", "2026-10-20", 800, "在线"),
    ("payments", "2026-11-01", 760, "在线"),
    ("payments", "2026-11-12", 790, "在线"),
    ("events", "2026-11-08", 5000, "在线"),
    ("events", "2026-11-12", 5200, "在线"),
    ("events", "2026-11-15", 5100, "在线"),
    ("inventory", "2026-10-30", 300, "在线"),
    ("inventory", "2026-11-11", 310, "在线"),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = _columns(conn, table)
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "judgments", JUDGMENT_COLUMNS)
    _add_columns(conn, "scan_jobs", SCAN_JOB_COLUMNS)
    _add_columns(conn, "retain_policies", POLICY_COLUMNS)

    # 同一主题域只保留最高版本为在用版，历史版本全部下线。
    domains = conn.execute(
        "SELECT domain_code, MAX(version) AS max_version "
        "FROM retain_policies GROUP BY domain_code"
    ).fetchall()
    for row in domains:
        conn.execute(
            "UPDATE retain_policies SET active = CASE WHEN version = ? THEN 1 ELSE 0 END "
            "WHERE domain_code = ?",
            (row["max_version"], row["domain_code"]),
        )

    # 历史遗留的重复开放裁决：同一分区只保留一条（执行中优先，其次最新），
    # 其余标记为已作废并写明原因，随后才能建部分唯一索引兜底并发开单。
    rows = conn.execute(
        "SELECT id, partition_id FROM judgments "
        "WHERE status IN ('待执行','执行中') "
        "ORDER BY partition_id, CASE status WHEN '执行中' THEN 0 ELSE 1 END, id DESC"
    ).fetchall()
    seen: set[int] = set()
    for row in rows:
        if row["partition_id"] in seen:
            conn.execute(
                "UPDATE judgments SET status = '已作废', disposition = '重扫作废', "
                "note = '历史重复开单清理（同一分区仅保留一条开放裁决）' WHERE id = ?",
                (row["id"],),
            )
        else:
            seen.add(row["partition_id"])

    # 崩溃遗留的「进行中」扫描任务不允许长期占坑；只回收超过 30 分钟的，
    # 避免启动时误伤另一个进程正在跑的扫描。
    conn.execute(
        "UPDATE scan_jobs SET status = '已中断', message = '扫描进程中断，占坑已释放' "
        "WHERE status = '进行中' AND created_at < datetime('now', '-30 minutes')"
    )

    # 进程中断遗留的「执行中」裁决回收为可重试，避免单条卡死后再也无法执行。
    conn.execute(
        "UPDATE judgments SET status = '已失败', "
        "fail_reason = '执行进程中断（重启回收），可重新执行' "
        "WHERE status = '执行中' AND created_at < datetime('now', '-30 minutes')"
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_judgments_open_partition "
        "ON judgments(partition_id) WHERE status IN ('待执行','执行中')"
    )
    # 扫描占坑：同一时刻全库只允许一个进行中的扫描任务持锁写库。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_job_running "
        "ON scan_jobs(status) WHERE status = '进行中'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_judgments_domain "
        "ON judgments(domain_code, policy_version)"
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM retain_policies").fetchone()["c"]
        if n == 0:
            for domain_code, domain_name, keep_days, action in SEED_POLICIES:
                conn.execute(
                    "INSERT INTO retain_policies "
                    "(domain_code, domain_name, keep_days, action_on_expire, "
                    "version, active, change_note) "
                    "VALUES (?,?,?,?,1,1,'基线版本')",
                    (domain_code, domain_name, keep_days, action),
                )
            for domain_code, biz_date, row_count, state in SEED_PARTITIONS:
                conn.execute(
                    "INSERT INTO partitions "
                    "(domain_code, biz_date, row_count, storage_state) "
                    "VALUES (?,?,?,?)",
                    (domain_code, biz_date, row_count, state),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('skeleton', ?)",
                (
                    "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
                ),
            )

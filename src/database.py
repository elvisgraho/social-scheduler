import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

DB_FILE = "data/scheduler.db"


def _ensure_db_dir() -> None:
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)


def get_conn() -> sqlite3.Connection:
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    _ensure_db_dir()
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            scheduled_for TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            title TEXT,
            description TEXT,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            platform_logs TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS account_state (
            platform TEXT PRIMARY KEY,
            connected INTEGER DEFAULT 0,
            last_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    _ensure_queue_columns(conn)
    conn.close()


def _ensure_queue_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(queue)")}
    columns = {
        "scheduled_for": "ALTER TABLE queue ADD COLUMN scheduled_for TEXT",
        "title": "ALTER TABLE queue ADD COLUMN title TEXT",
        "description": "ALTER TABLE queue ADD COLUMN description TEXT",
        "attempts": "ALTER TABLE queue ADD COLUMN attempts INTEGER DEFAULT 0",
        "last_error": "ALTER TABLE queue ADD COLUMN last_error TEXT",
        "platform_logs": "ALTER TABLE queue ADD COLUMN platform_logs TEXT",
    }
    for column, ddl in columns.items():
        if column not in existing:
            conn.execute(ddl)
    conn.commit()


def set_config(key: str, value: Any) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_config(key: str, default: Optional[Any] = None) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_json_config(key: str, payload: Dict[str, Any]) -> None:
    set_config(key, json.dumps(payload))


def get_json_config(key: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = get_config(key)
    if not raw:
        return default or {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default or {}


def add_to_queue(
    file_path: str,
    scheduled_for: Optional[str],
    title: Optional[str],
    description: Optional[str],
) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO queue (file_path, scheduled_for, title, description)
        VALUES (?, ?, ?, ?)
        """,
        (file_path, scheduled_for, title, description),
    )
    conn.commit()
    vid = cur.lastrowid
    conn.close()
    return vid


def add_many_to_queue(entries: Iterable[Dict[str, Any]]) -> List[int]:
    payload = list(entries)
    if not payload:
        return []
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO queue (file_path, scheduled_for, title, description)
        VALUES (:file_path, :scheduled_for, :title, :description)
        """,
        payload,
    )
    conn.commit()
    last_id = cur.lastrowid or 0
    conn.close()
    first_id = last_id - len(payload) + 1
    return list(range(first_id, last_id + 1))


def get_queue(limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM queue
        ORDER BY
            CASE WHEN scheduled_for IS NULL THEN 1 ELSE 0 END,
            scheduled_for ASC,
            id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_due_queue(now_iso: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM queue
        WHERE status IN ('pending', 'retry')
        AND (scheduled_for IS NULL OR scheduled_for <= ?)
        ORDER BY
            CASE WHEN scheduled_for IS NULL THEN 1 ELSE 0 END,
            scheduled_for ASC,
            id ASC
        """,
        (now_iso,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_queue_item(queue_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (queue_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def increment_attempts(queue_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE queue SET attempts = attempts + 1 WHERE id = ?", (queue_id,)
    )
    conn.commit()
    conn.close()


def update_queue_status(
    queue_id: int,
    status: str,
    last_error: Optional[str] = None,
    platform_logs: Optional[Dict[str, Any]] = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE queue
        SET status = ?, last_error = ?, platform_logs = ?
        WHERE id = ?
        """,
        (
            status,
            last_error,
            json.dumps(platform_logs or {}),
            queue_id,
        ),
    )
    conn.commit()
    conn.close()


def reschedule_queue_item(queue_id: int, scheduled_for: Optional[str]) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE queue SET scheduled_for = ? WHERE id = ?",
        (scheduled_for, queue_id),
    )
    conn.commit()
    conn.close()


def delete_from_queue(queue_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
    conn.commit()
    conn.close()


def set_account_state(platform: str, connected: bool, last_error: Optional[str]) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO account_state (platform, connected, last_error, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(platform) DO UPDATE SET
            connected = excluded.connected,
            last_error = excluded.last_error,
            updated_at = CURRENT_TIMESTAMP
        """,
        (platform, int(bool(connected)), last_error),
    )
    conn.commit()
    conn.close()


def get_account_state(platform: str) -> Dict[str, Any]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM account_state WHERE platform = ?", (platform,)
    ).fetchone()
    conn.close()
    if not row:
        return {"platform": platform, "connected": 0, "last_error": None, "updated_at": None}
    return dict(row)


def get_all_account_states() -> Dict[str, Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM account_state").fetchall()
    conn.close()
    return {row["platform"]: dict(row) for row in rows}

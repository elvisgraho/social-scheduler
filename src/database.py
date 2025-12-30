import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    _ensure_uploads_table(conn)
    _ensure_queue_columns(conn)
    _migrate_uploaded_rows(conn)
    conn.close()


def _ensure_queue_columns(conn: sqlite3.Connection) -> None:
    """
    Migration helper to ensure existing databases get new columns.
    """
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


def _ensure_uploads_table(conn: sqlite3.Connection) -> None:
    """
    New table to store completed uploads separately from the active queue.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER,
            file_path TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            title TEXT,
            description TEXT,
            platform_logs TEXT
        )
        """
    )
    conn.commit()


def _migrate_uploaded_rows(conn: sqlite3.Connection) -> None:
    """
    Move any legacy queue rows with status='uploaded' into the uploads table.
    """
    try:
        rows = conn.execute("SELECT * FROM queue WHERE status = 'uploaded'").fetchall()
        if not rows:
            return

        for row in rows:
            row_dict = dict(row)
            conn.execute(
                """
                INSERT INTO uploads (queue_id, file_path, title, description, platform_logs)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row_dict.get("id"),
                    row_dict.get("file_path"),
                    row_dict.get("title"),
                    row_dict.get("description"),
                    row_dict.get("platform_logs"),
                ),
            )
            conn.execute("DELETE FROM queue WHERE id = ?", (row_dict.get("id"),))
        conn.commit()
    except Exception:
        # Best-effort migration; do not block startup
        conn.rollback()
        pass


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
    except (json.JSONDecodeError, TypeError):
        # Fallback for corrupted JSON or empty strings
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
    # Estimate ID range
    first_id = last_id - len(payload) + 1
    return list(range(first_id, last_id + 1))


def get_queue(limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM queue
        WHERE status != 'uploaded'
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
    # Prioritize items that are pending/retry and whose schedule time has passed
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


def cleanup_uploaded(count: int) -> Tuple[int, int]:
    """
    Delete the oldest uploaded items and remove their files from disk.
    Returns (items_deleted, bytes_freed).
    """
    import os

    items = get_uploaded_items(count)
    deleted = 0
    freed_bytes = 0
    for row in items:
        file_path = row.get("file_path")
        if file_path and os.path.exists(file_path):
            try:
                freed_bytes += os.path.getsize(file_path)
                os.remove(file_path)
            except Exception:
                pass
        delete_uploaded_item(row["id"])
        deleted += 1
    return deleted, freed_bytes


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


# --- Upload Archive ---

def archive_uploaded_item(queue_row: Dict[str, Any], platform_logs: Optional[Dict[str, Any]]) -> None:
    """
    Persist completed uploads to the uploads table and remove from the active queue.
    """
    conn = get_conn()
    logs_json = json.dumps(platform_logs or {})
    uploaded_at = datetime.utcnow().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO uploads (queue_id, file_path, uploaded_at, title, description, platform_logs)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                queue_row.get("id"),
                queue_row.get("file_path"),
                uploaded_at,
                queue_row.get("title"),
                queue_row.get("description"),
                logs_json,
            ),
        )
        conn.execute("DELETE FROM queue WHERE id = ?", (queue_row.get("id"),))
        conn.commit()
    finally:
        conn.close()


def delete_uploaded_item(upload_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    conn.commit()
    conn.close()


def get_uploaded_items(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Return the oldest uploaded items first so cleanup can prune them.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM uploads
        ORDER BY uploaded_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_uploaded_count() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM uploads").fetchone()
    conn.close()
    return row["cnt"] if row else 0


# --- Backup & Restore ---

def get_all_settings() -> Dict[str, Any]:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


def export_config() -> Dict[str, Any]:
    """
    Export settings and account_state for backup/migration.
    """
    return {
        "settings": get_all_settings(),
        "account_state": get_all_account_states(),
    }


def import_config(payload: Dict[str, Any]) -> Tuple[int, int]:
    """
    Restore settings/account_state from a backup payload.
    Returns (settings_count, account_state_count).
    """
    settings = payload.get("settings") or {}
    accounts = payload.get("account_state") or {}

    for key, value in settings.items():
        set_config(key, value)

    for platform, state in accounts.items():
        set_account_state(platform, bool(state.get("connected")), state.get("last_error"))

    return len(settings), len(accounts)


def clear_platform_status(queue_id: int, platform_key: str) -> bool:
    """
    Clear a specific platform's status from platform_logs to allow retry.
    Returns True if successful, False otherwise.
    """
    conn = get_conn()
    try:
        row = conn.execute("SELECT platform_logs FROM queue WHERE id = ?", (queue_id,)).fetchone()
        if not row:
            return False
        
        logs = {}
        raw_logs = row["platform_logs"]
        if raw_logs:
            try:
                logs = json.loads(raw_logs) if isinstance(raw_logs, str) else raw_logs
            except (json.JSONDecodeError, TypeError):
                logs = {}
        
        # Clear the specific platform status
        if platform_key in logs:
            del logs[platform_key]
            
            conn.execute(
                "UPDATE queue SET platform_logs = ? WHERE id = ?",
                (json.dumps(logs), queue_id)
            )
            conn.commit()
            return True
        return False
    finally:
        conn.close()

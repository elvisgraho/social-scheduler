"""
Database module for Social Scheduler.

Provides SQLite-based persistence with connection pooling optimizations,
WAL mode for concurrency, and context manager support for safe resource handling.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

DB_FILE = "data/scheduler.db"


def _ensure_db_dir() -> None:
    """Ensure the database directory exists."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for database connections with optimization settings.
    
    Usage:
        with get_conn() as conn:
            conn.execute("SELECT * FROM queue")
    
    Yields:
        sqlite3.Connection: Configured database connection
    """
    _ensure_db_dir()
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=10.0,  # Wait up to 10 seconds for locks
        isolation_level='DEFERRED'  # Better concurrency
    )
    conn.row_factory = sqlite3.Row

    # Performance optimizations
    conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
    conn.execute("PRAGMA synchronous=NORMAL")  # Balance between safety and speed
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")  # Store temp tables in memory

    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initialize database tables and run migrations."""
    _ensure_db_dir()
    with get_conn() as conn:
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
        "enabled_platforms": "ALTER TABLE queue ADD COLUMN enabled_platforms TEXT",
        "platform_overrides": "ALTER TABLE queue ADD COLUMN platform_overrides TEXT",
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
            platform_logs TEXT,
            enabled_platforms TEXT,
            platform_overrides TEXT
        )
        """
    )
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)")}
    upload_columns = {
        "enabled_platforms": "ALTER TABLE uploads ADD COLUMN enabled_platforms TEXT",
        "platform_overrides": "ALTER TABLE uploads ADD COLUMN platform_overrides TEXT",
    }
    for column, ddl in upload_columns.items():
        if column not in existing:
            conn.execute(ddl)
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
                INSERT INTO uploads (
                    queue_id,
                    file_path,
                    title,
                    description,
                    platform_logs,
                    enabled_platforms,
                    platform_overrides
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_dict.get("id"),
                    row_dict.get("file_path"),
                    row_dict.get("title"),
                    row_dict.get("description"),
                    row_dict.get("platform_logs"),
                    row_dict.get("enabled_platforms"),
                    row_dict.get("platform_overrides"),
                ),
            )
            conn.execute("DELETE FROM queue WHERE id = ?", (row_dict.get("id"),))
        conn.commit()
    except Exception:
        # Best-effort migration; do not block startup
        conn.rollback()
        pass


def set_config(key: str, value: Any) -> None:
    """Store a configuration value in the database."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()


def get_config(key: str, default: Optional[Any] = None) -> Optional[str]:
    """Retrieve a configuration value from the database."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_json_config(key: str, payload: Dict[str, Any]) -> None:
    """Store a JSON-serializable configuration value."""
    set_config(key, json.dumps(payload))


def get_json_config(key: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Retrieve and parse a JSON configuration value."""
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
    enabled_platforms: Optional[str] = None,
    platform_overrides: Optional[str] = None,
) -> int:
    """Add a single item to the upload queue. Returns the new queue item ID."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO queue (file_path, scheduled_for, title, description, enabled_platforms, platform_overrides)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_path, scheduled_for, title, description, enabled_platforms, platform_overrides),
        )
        conn.commit()
        return cur.lastrowid


def add_many_to_queue(entries: Iterable[Dict[str, Any]]) -> List[int]:
    """Add multiple items to the queue efficiently. Returns list of new queue item IDs."""
    payload = list(entries)
    if not payload:
        return []
    with get_conn() as conn:
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO queue (file_path, scheduled_for, title, description, enabled_platforms, platform_overrides)
            VALUES (:file_path, :scheduled_for, :title, :description, :enabled_platforms, :platform_overrides)
            """,
            payload,
        )
        conn.commit()
        last_id = cur.lastrowid or 0
        # Estimate ID range
        first_id = last_id - len(payload) + 1
        return list(range(first_id, last_id + 1))


def get_queue(limit: int = 100) -> List[Dict[str, Any]]:
    """Get all queue items excluding uploaded ones, ordered by schedule."""
    with get_conn() as conn:
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
        return [dict(row) for row in rows]


def get_due_queue(now_iso: str) -> List[Dict[str, Any]]:
    """Get queue items that are due for processing (pending/retry and past scheduled time)."""
    with get_conn() as conn:
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
        return [dict(row) for row in rows]


def get_queue_item(queue_id: int) -> Optional[Dict[str, Any]]:
    """Get a single queue item by ID."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM queue WHERE id = ?", (queue_id,)).fetchone()
        return dict(row) if row else None


def increment_attempts(queue_id: int) -> None:
    """Increment the attempt counter for a queue item."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE queue SET attempts = attempts + 1 WHERE id = ?", (queue_id,)
        )
        conn.commit()


def _serialize_platform_logs(platform_logs: Optional[Any]) -> str:
    """Serialize platform logs to JSON string, handling edge cases."""
    if not platform_logs:
        return "{}"
    if isinstance(platform_logs, str):
        # Already a JSON string, validate it's valid JSON
        try:
            json.loads(platform_logs)
            return platform_logs
        except json.JSONDecodeError:
            return "{}"
    if isinstance(platform_logs, dict):
        return json.dumps(platform_logs)
    return "{}"


def update_queue_status(
    queue_id: int,
    status: str,
    last_error: Optional[str] = None,
    platform_logs: Optional[Dict[str, Any]] = None,
) -> None:
    """Update the status of a queue item."""
    logs_json = _serialize_platform_logs(platform_logs)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE queue
            SET status = ?, last_error = ?, platform_logs = ?
            WHERE id = ?
            """,
            (status, last_error, logs_json, queue_id),
        )
        conn.commit()


def reschedule_queue_item(queue_id: int, scheduled_for: Optional[str]) -> None:
    """Update the scheduled time for a queue item."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE queue SET scheduled_for = ? WHERE id = ?",
            (scheduled_for, queue_id),
        )
        conn.commit()


def delete_from_queue(queue_id: int) -> None:
    """Remove an item from the queue."""
    with get_conn() as conn:
        conn.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
        conn.commit()


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
    """Update the connection state for a platform."""
    with get_conn() as conn:
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


def get_account_state(platform: str) -> Dict[str, Any]:
    """Get the connection state for a platform."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM account_state WHERE platform = ?", (platform,)
        ).fetchone()
        if not row:
            return {"platform": platform, "connected": 0, "last_error": None, "updated_at": None}
        return dict(row)


def get_all_account_states() -> Dict[str, Dict[str, Any]]:
    """Get all platform connection states."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM account_state").fetchall()
        return {row["platform"]: dict(row) for row in rows}


# --- Upload Archive ---

def archive_uploaded_item(queue_row: Dict[str, Any], platform_logs: Optional[Dict[str, Any]]) -> None:
    """
    Persist completed uploads to the uploads table and remove from the active queue.
    """
    logs_json = json.dumps(platform_logs or {})
    uploaded_at = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO uploads (
                queue_id,
                file_path,
                uploaded_at,
                title,
                description,
                platform_logs,
                enabled_platforms,
                platform_overrides
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_row.get("id"),
                queue_row.get("file_path"),
                uploaded_at,
                queue_row.get("title"),
                queue_row.get("description"),
                logs_json,
                queue_row.get("enabled_platforms"),
                queue_row.get("platform_overrides"),
            ),
        )
        conn.execute("DELETE FROM queue WHERE id = ?", (queue_row.get("id"),))
        conn.commit()


def delete_uploaded_item(upload_id: int) -> None:
    """Delete an uploaded item record."""
    with get_conn() as conn:
        conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        conn.commit()


def get_uploaded_items(limit: int = 100) -> List[Dict[str, Any]]:
    """Return uploaded items, most recent first."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM uploads
            ORDER BY uploaded_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_uploaded_count() -> int:
    """Get the total count of uploaded items."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM uploads").fetchone()
        return row["cnt"] if row else 0


def get_pending_count() -> int:
    """Get the count of active (pending/retry) queue items without fetching all rows."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM queue WHERE status IN ('pending', 'retry')"
        ).fetchone()
        return row["cnt"] if row else 0


# --- Backup & Restore ---

def get_all_settings() -> Dict[str, Any]:
    """Get all settings as a dictionary."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def export_config() -> Dict[str, Any]:
    """Export settings and account_state for backup/migration."""
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
    with get_conn() as conn:
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

        # Clear the specific platform status (if it exists)
        if platform_key in logs:
            del logs[platform_key]

        # Always update and return True (even if key didn't exist)
        # This allows forcing uploads for platforms that haven't been attempted yet
        conn.execute(
            "UPDATE queue SET platform_logs = ? WHERE id = ?",
            (json.dumps(logs), queue_id)
        )
        conn.commit()
        return True


def restore_archived_to_queue(upload_id: int) -> int:
    """
    Restore an archived upload back to the queue with 'failed' status.
    Returns the new queue item ID if successful, 0 otherwise.
    """
    with get_conn() as conn:
        try:
            # Get the archived upload
            row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
            if not row:
                return 0

            upload_dict = dict(row)

            # Insert back into queue with 'failed' status
            cursor = conn.execute(
                """
                INSERT INTO queue (
                    file_path,
                    scheduled_for,
                    title,
                    description,
                    platform_logs,
                    enabled_platforms,
                    platform_overrides,
                    status,
                    attempts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', 0)
                """,
                (
                    upload_dict.get("file_path"),
                    datetime.now(timezone.utc).isoformat(),  # Schedule for now
                    upload_dict.get("title"),
                    upload_dict.get("description"),
                    upload_dict.get("platform_logs"),
                    upload_dict.get("enabled_platforms"),
                    upload_dict.get("platform_overrides"),
                ),
            )
            new_queue_id = cursor.lastrowid or 0

            # Delete from uploads table
            conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
            conn.commit()
            return new_queue_id
        except Exception:
            conn.rollback()
            return 0

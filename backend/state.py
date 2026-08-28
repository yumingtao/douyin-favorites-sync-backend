"""SQLite-backed persistent stores for history, content index, and backend config.

Drop-in replacement for the previous JSON file stores — same public interfaces.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          TEXT PRIMARY KEY,
    time        TEXT NOT NULL,
    type        TEXT DEFAULT '',
    status      TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    details     TEXT DEFAULT '{}',
    retry_status      TEXT DEFAULT '',
    retry_history_id  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS content_index (
    douyin_id   TEXT PRIMARY KEY,
    data        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS backend_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _DB:
    """Thread-safe SQLite connection wrapper."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, params)


class HistoryStore:
    def __init__(self, root: Path):
        self._db = _DB(root / "douyin-sync.db")
        self._migrate_from_json(root)

    def _migrate_from_json(self, root: Path) -> None:
        legacy = root / "backend-history.json"
        if not legacy.exists():
            return
        count = self._db.execute("SELECT COUNT(*) as c FROM history").fetchone()["c"]
        if count > 0:
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        rows = []
        for event in data:
            rows.append((
                event.get("id", ""),
                event.get("time", ""),
                event.get("type", ""),
                event.get("status", ""),
                event.get("summary", ""),
                event.get("url", ""),
                json.dumps(event.get("details", {}), ensure_ascii=False),
                event.get("retry_status", ""),
                event.get("retry_history_id", ""),
            ))
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO history "
                "(id, time, type, status, summary, url, details, retry_status, retry_history_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def read(self) -> list[dict[str, Any]]:
        cur = self._db.execute("SELECT * FROM history ORDER BY rowid DESC")
        return [self._row_to_event(r) for r in cur.fetchall()]

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        event: dict[str, Any] = {
            "id": row["id"],
            "time": row["time"],
            "type": row["type"],
            "status": row["status"],
            "summary": row["summary"],
            "url": row["url"],
            "details": json.loads(row["details"] or "{}"),
        }
        if row["retry_status"]:
            event["retry_status"] = row["retry_status"]
        if row["retry_history_id"]:
            event["retry_history_id"] = row["retry_history_id"]
        return event

    def get(self, event_id: str) -> dict[str, Any] | None:
        cur = self._db.execute("SELECT * FROM history WHERE id = ?", (event_id,))
        row = cur.fetchone()
        return self._row_to_event(row) if row else None

    def query(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        type_filter: str = "",
        status_filter: str = "",
        q: str = "",
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = min(100, max(1, page_size))

        conditions: list[str] = []
        params: list[Any] = []
        if type_filter:
            conditions.append("type = ?")
            params.append(type_filter)
        if status_filter:
            if status_filter == "retried_ok":
                # Records that were originally error but retry succeeded
                conditions.append(
                    "status = 'error' AND retry_status IN ('ok','duplicate')"
                )
            elif status_filter == "retried_error":
                # Records that are still error after retry
                conditions.append(
                    "retry_status = 'error'"
                )
            elif status_filter == "no_retry_error":
                # Error records that haven't been retried
                conditions.append(
                    "status = 'error' AND (retry_status = '' OR retry_status IS NULL)"
                )
            else:
                # Effective status: retry_status 'ok'/'duplicate' upgrades to 'ok'
                conditions.append(
                    "CASE WHEN retry_status IN ('ok','duplicate') THEN 'ok' ELSE status END = ?"
                )
                params.append(status_filter)
        if q.strip():
            conditions.append("(summary LIKE ? OR url LIKE ? OR details LIKE ?)")
            needle = f"%{q.strip()}%"
            params.extend([needle, needle, needle])

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_sql = f"SELECT COUNT(*) as c FROM history {where}"
        total = self._db.execute(count_sql, tuple(params)).fetchone()["c"]

        pages = max(1, math.ceil(total / page_size))
        page = min(page, pages)
        offset = (page - 1) * page_size

        data_sql = (
            f"SELECT * FROM history {where} "
            f"ORDER BY rowid DESC LIMIT ? OFFSET ?"
        )
        cur = self._db.execute(data_sql, tuple(params + [page_size, offset]))
        items = [self._row_to_event(r) for r in cur.fetchall()]

        types = [r[0] for r in self._db.execute(
            "SELECT DISTINCT type FROM history WHERE type != '' ORDER BY type"
        ).fetchall()]
        statuses = [r[0] for r in self._db.execute(
            "SELECT DISTINCT status FROM history WHERE status != '' ORDER BY status"
        ).fetchall()]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "types": types,
            "statuses": statuses,
        }

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        all_count = self._db.execute("SELECT COUNT(*) as c FROM history").fetchone()["c"]
        record = {
            "id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{all_count + 1}",
            "time": local_now(),
            **event,
        }
        self._db.execute(
            "INSERT INTO history (id, time, type, status, summary, url, details, retry_status, retry_history_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '', '')",
            (
                record["id"],
                record["time"],
                record.get("type", ""),
                record.get("status", ""),
                record.get("summary", ""),
                record.get("url", ""),
                json.dumps(record.get("details", {}), ensure_ascii=False),
            ),
        )
        return record

    def mark_retry(self, event_id: str, *, retry_status: str, retry_history_id: str = "") -> bool:
        cur = self._db.execute("SELECT id FROM history WHERE id = ?", (event_id,))
        if not cur.fetchone():
            return False
        if retry_history_id:
            self._db.execute(
                "UPDATE history SET retry_status = ?, retry_history_id = ? WHERE id = ?",
                (retry_status, retry_history_id, event_id),
            )
        else:
            self._db.execute(
                "UPDATE history SET retry_status = ? WHERE id = ?",
                (retry_status, event_id),
            )
        return True

    def delete(self, event_id: str) -> bool:
        cur = self._db.execute("SELECT id FROM history WHERE id = ?", (event_id,))
        if not cur.fetchone():
            return False
        self._db.execute("DELETE FROM history WHERE id = ?", (event_id,))
        return True


class ContentIndexStore:
    def __init__(self, root: Path):
        self._db = _DB(root / "douyin-sync.db")
        self._migrate_from_json(root)

    def _migrate_from_json(self, root: Path) -> None:
        legacy = root / "content-index.json"
        if not legacy.exists():
            return
        count = self._db.execute("SELECT COUNT(*) as c FROM content_index").fetchone()["c"]
        if count > 0:
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        rows = [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items() if isinstance(v, (dict, list, str))]
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO content_index (douyin_id, data) VALUES (?, ?)",
                rows,
            )

    def read(self) -> dict[str, Any]:
        cur = self._db.execute("SELECT douyin_id, data FROM content_index")
        return {
            row["douyin_id"]: json.loads(row["data"] or "{}")
            for row in cur.fetchall()
        }

    def get(self, douyin_id: str) -> dict[str, Any] | None:
        cur = self._db.execute(
            "SELECT data FROM content_index WHERE douyin_id = ?", (douyin_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        record = json.loads(row["data"] or "{}")
        return record if isinstance(record, dict) else None

    def upsert(self, douyin_id: str, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.get(douyin_id) or {}
        merged = {
            **existing,
            **record,
            "douyin_id": douyin_id,
            "updated_at": local_now(),
        }
        self._db.execute(
            "INSERT OR REPLACE INTO content_index (douyin_id, data) VALUES (?, ?)",
            (douyin_id, json.dumps(merged, ensure_ascii=False)),
        )
        return merged


class BackendConfigStore:
    def __init__(self, root: Path):
        self._db = _DB(root / "douyin-sync.db")
        self._migrate_from_json(root)

    def _migrate_from_json(self, root: Path) -> None:
        legacy = root / "backend-config.json"
        if not legacy.exists():
            return
        count = self._db.execute("SELECT COUNT(*) as c FROM backend_config").fetchone()["c"]
        if count > 0:
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        rows = [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in data.items()]
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO backend_config (key, value) VALUES (?, ?)",
                rows,
            )

    def read(self) -> dict[str, Any]:
        cur = self._db.execute("SELECT key, value FROM backend_config")
        return {
            row["key"]: json.loads(row["value"])
            for row in cur.fetchall()
        }

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        data = {**self.read(), **values, "updated_at": local_now()}
        self._db.execute("DELETE FROM backend_config")
        self._db.executemany(
            "INSERT INTO backend_config (key, value) VALUES (?, ?)",
            [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in data.items()],
        )
        return data

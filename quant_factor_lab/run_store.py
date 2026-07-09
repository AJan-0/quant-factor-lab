from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunRecord:
    id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    config_hash: str
    output_dir: str
    summary: dict[str, Any] | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "configHash": self.config_hash,
            "outputDir": self.output_dir,
            "summary": self.summary,
            "error": self.error,
        }


@dataclass(frozen=True)
class RunLogRecord:
    id: int
    run_id: str
    timestamp: str
    level: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runId": self.run_id,
            "timestamp": self.timestamp,
            "level": self.level,
            "message": self.message,
        }


class RunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def create_run(self, run_id: str, config_hash: str, output_dir: str) -> RunRecord:
        created_at = _utc_now()
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runs (id, status, created_at, config_hash, output_dir)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, "queued", created_at, config_hash, output_dir),
            )
        record = self.get_run(run_id)
        assert record is not None
        return record

    def mark_running(self, run_id: str) -> None:
        self._update_run(
            run_id,
            status="running",
            started_at=_utc_now(),
            error=None,
        )

    def mark_succeeded(self, run_id: str, summary: dict[str, Any]) -> None:
        self._update_run(
            run_id,
            status="succeeded",
            finished_at=_utc_now(),
            summary_json=json.dumps(_json_ready(summary), ensure_ascii=False, sort_keys=True),
            error=None,
        )

    def mark_failed(self, run_id: str, error: str) -> None:
        self._update_run(
            run_id,
            status="failed",
            finished_at=_utc_now(),
            summary_json=None,
            error=error,
        )

    def mark_canceled(self, run_id: str, reason: str = "cancelled by user") -> None:
        self._update_run(
            run_id,
            status="canceled",
            finished_at=_utc_now(),
            summary_json=None,
            error=reason,
        )

    def append_log(self, run_id: str, level: str, message: str) -> RunLogRecord:
        timestamp = _utc_now()
        safe_level = str(level or "INFO").upper()[:16]
        safe_message = str(message)
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO run_logs (run_id, timestamp, level, message)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, timestamp, safe_level, safe_message),
            )
            row_id = int(cursor.lastrowid)
        return RunLogRecord(
            id=row_id,
            run_id=run_id,
            timestamp=timestamp,
            level=safe_level,
            message=safe_message,
        )

    def list_logs(self, run_id: str, limit: int = 500, after_id: int | None = None) -> list[RunLogRecord]:
        safe_limit = max(1, min(int(limit), 2000))
        with self._lock, closing(self._connect()) as connection, connection:
            if after_id is None:
                rows = connection.execute(
                    "SELECT * FROM run_logs WHERE run_id = ? ORDER BY id ASC LIMIT ?",
                    (run_id, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM run_logs WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (run_id, int(after_id), safe_limit),
                ).fetchall()
        return [_row_to_log(row) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _update_run(self, run_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = list(fields.values())
        values.append(run_id)
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown run id: {run_id}")

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    config_hash TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    summary_json TEXT,
                    error TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_run_logs_run_id_id ON run_logs(run_id, id)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def hash_config(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    summary = json.loads(row["summary_json"]) if row["summary_json"] else None
    return RunRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        config_hash=str(row["config_hash"]),
        output_dir=str(row["output_dir"]),
        summary=summary,
        error=row["error"],
    )


def _row_to_log(row: sqlite3.Row) -> RunLogRecord:
    return RunLogRecord(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        timestamp=str(row["timestamp"]),
        level=str(row["level"]),
        message=str(row["message"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if hasattr(value, "item"):
        return _json_ready(value.item())
    return value

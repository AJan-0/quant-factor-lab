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

import pandas as pd


RAW_SNAPSHOT_COLUMNS = [
    "snapshot_id",
    "run_id",
    "dataset",
    "provider",
    "symbols",
    "frequency",
    "rows",
    "start_time",
    "end_time",
    "content_hash",
    "artifact_path",
    "fetched_at",
]


@dataclass(frozen=True)
class RawDataSnapshot:
    snapshot_id: str
    run_id: str
    dataset: str
    provider: str
    symbols: list[str]
    frequency: str | None
    rows: int
    start_time: str | None
    end_time: str | None
    content_hash: str
    artifact_path: str
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "dataset": self.dataset,
            "provider": self.provider,
            "symbols": self.symbols,
            "frequency": self.frequency,
            "rows": self.rows,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "content_hash": self.content_hash,
            "artifact_path": self.artifact_path,
            "fetched_at": self.fetched_at,
        }


class RawDataVersionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def record_frame(
        self,
        *,
        run_id: str,
        dataset: str,
        provider: str,
        frame: pd.DataFrame,
        artifact_path: str | Path,
        frequency: str | None = None,
    ) -> RawDataSnapshot:
        content_hash = dataframe_content_hash(frame)
        fetched_at = _utc_now()
        symbols = _symbols(frame)
        start_time, end_time = _time_range(frame)
        snapshot_id = hashlib.sha256(
            json.dumps(
                {
                    "run_id": run_id,
                    "dataset": dataset,
                    "provider": provider,
                    "content_hash": content_hash,
                    "artifact_path": str(artifact_path),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        snapshot = RawDataSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            dataset=dataset,
            provider=provider,
            symbols=symbols,
            frequency=frequency,
            rows=int(len(frame)),
            start_time=start_time,
            end_time=end_time,
            content_hash=content_hash,
            artifact_path=str(artifact_path),
            fetched_at=fetched_at,
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO raw_snapshots (
                    snapshot_id, run_id, dataset, provider, symbols_json, frequency,
                    rows, start_time, end_time, content_hash, artifact_path, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.run_id,
                    snapshot.dataset,
                    snapshot.provider,
                    json.dumps(snapshot.symbols, ensure_ascii=False),
                    snapshot.frequency,
                    snapshot.rows,
                    snapshot.start_time,
                    snapshot.end_time,
                    snapshot.content_hash,
                    snapshot.artifact_path,
                    snapshot.fetched_at,
                ),
            )
        return snapshot

    def list_snapshots(self, limit: int = 200, run_id: str | None = None) -> list[RawDataSnapshot]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock, closing(self._connect()) as connection, connection:
            if run_id:
                rows = connection.execute(
                    "SELECT * FROM raw_snapshots WHERE run_id = ? ORDER BY fetched_at DESC LIMIT ?",
                    (run_id, safe_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM raw_snapshots ORDER BY fetched_at DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [_row_to_snapshot(row) for row in rows]

    def _ensure_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    frequency TEXT,
                    rows INTEGER NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    content_hash TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_raw_snapshots_run_id ON raw_snapshots(run_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_raw_snapshots_fetched_at ON raw_snapshots(fetched_at DESC)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def dataframe_content_hash(frame: pd.DataFrame) -> str:
    if frame.empty:
        payload = b"EMPTY"
    else:
        stable = frame.copy()
        stable = stable.reindex(sorted(stable.columns), axis=1)
        payload = stable.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def raw_snapshot_frame(snapshots: list[RawDataSnapshot]) -> pd.DataFrame:
    rows = [snapshot.to_dict() for snapshot in snapshots]
    if not rows:
        return pd.DataFrame(columns=RAW_SNAPSHOT_COLUMNS)
    frame = pd.DataFrame(rows)
    frame["symbols"] = frame["symbols"].map(lambda value: ",".join(value) if isinstance(value, list) else value)
    return frame[RAW_SNAPSHOT_COLUMNS]


def _symbols(frame: pd.DataFrame) -> list[str]:
    if frame.empty or "symbol" not in frame.columns:
        return []
    return sorted(str(symbol) for symbol in frame["symbol"].dropna().unique())


def _time_range(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    if frame.empty or "timestamp" not in frame.columns:
        return None, None
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce").dropna()
    if timestamps.empty:
        return None, None
    return str(timestamps.min()), str(timestamps.max())


def _row_to_snapshot(row: sqlite3.Row) -> RawDataSnapshot:
    return RawDataSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        run_id=str(row["run_id"]),
        dataset=str(row["dataset"]),
        provider=str(row["provider"]),
        symbols=json.loads(row["symbols_json"]),
        frequency=row["frequency"],
        rows=int(row["rows"]),
        start_time=row["start_time"],
        end_time=row["end_time"],
        content_hash=str(row["content_hash"]),
        artifact_path=str(row["artifact_path"]),
        fetched_at=str(row["fetched_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

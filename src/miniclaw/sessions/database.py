import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.sessions.types import (
    CheckpointIndex,
    RunRecord,
    SessionRecord,
)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamps must be UTC")
    return value.isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SessionRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    latest_checkpoint_id TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_executions (
                    tool_execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    tool_call_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotent INTEGER NOT NULL,
                    result_json TEXT
                );
                """
            )

    async def create_session(self, session: SessionRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (
                    session.session_id,
                    _timestamp(session.created_at),
                    _timestamp(session.updated_at),
                ),
            )

    async def create_run(self, run: RunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.session_id,
                    run.status,
                    _timestamp(run.started_at),
                    _timestamp(run.updated_at),
                    run.latest_checkpoint_id,
                ),
            )

    async def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunRecord(
            row["run_id"],
            row["session_id"],
            row["status"],
            _datetime(row["started_at"]),
            _datetime(row["updated_at"]),
            row["latest_checkpoint_id"],
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            row["session_id"],
            _datetime(row["created_at"]),
            _datetime(row["updated_at"]),
        )

    async def latest_run_for_session(self, session_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE session_id = ?
                ORDER BY started_at DESC, run_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return RunRecord(
            row["run_id"],
            row["session_id"],
            row["status"],
            _datetime(row["started_at"]),
            _datetime(row["updated_at"]),
            row["latest_checkpoint_id"],
        )

    async def touch_session(self, session_id: str, updated_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (_timestamp(updated_at), session_id),
            )

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, session_id"
            ).fetchall()
        return tuple(
            SessionRecord(
                row["session_id"],
                _datetime(row["created_at"]),
                _datetime(row["updated_at"]),
            )
            for row in rows
        )

    async def list_runs(self) -> tuple[RunRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC, run_id"
            ).fetchall()
        return tuple(
            RunRecord(
                row["run_id"],
                row["session_id"],
                row["status"],
                _datetime(row["started_at"]),
                _datetime(row["updated_at"]),
                row["latest_checkpoint_id"],
            )
            for row in rows
        )

    async def record_checkpoint(
        self,
        *,
        checkpoint_id: str,
        run_id: str,
        sequence: int,
        path: Path,
        sha256: str,
        created_at: datetime,
        run_status: str,
    ) -> None:
        timestamp = _timestamp(created_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                (checkpoint_id, run_id, sequence, str(path), sha256, timestamp),
            )
            connection.execute(
                """
                UPDATE runs
                SET latest_checkpoint_id = ?, status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (checkpoint_id, run_status, timestamp, run_id),
            )
            connection.commit()

    async def checkpoint_index(
        self,
        checkpoint_id: str,
    ) -> CheckpointIndex | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._decode_index(row)

    async def latest_checkpoint_index(
        self,
        run_id: str,
    ) -> CheckpointIndex | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT checkpoints.*
                FROM runs
                JOIN checkpoints
                  ON checkpoints.checkpoint_id = runs.latest_checkpoint_id
                WHERE runs.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._decode_index(row)

    @staticmethod
    def _decode_index(row: sqlite3.Row | None) -> CheckpointIndex | None:
        if row is None:
            return None
        return CheckpointIndex(
            row["checkpoint_id"],
            row["run_id"],
            row["sequence"],
            Path(row["path"]),
            row["sha256"],
            _datetime(row["created_at"]),
        )


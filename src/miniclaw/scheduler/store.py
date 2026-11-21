import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.scheduler.types import ScheduledTask, ScheduledTaskStatus


class SchedulerStore:
    def __init__(self, database_path: Path, *, workspace: Path | None = None) -> None:
        self.database_path = database_path
        self.workspace = str(workspace.resolve()) if workspace else "__standalone__"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        return await asyncio.to_thread(self._initialize_blocking)

    def _initialize_blocking(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_text TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    idempotent INTEGER NOT NULL,
                    parent_task_id TEXT,
                    child_run_id TEXT,
                    last_error TEXT
                )
                """
            )
            # Added after the original schema; existing databases keep working.
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(scheduled_tasks)"
                ).fetchall()
            }
            if "owner" not in columns:
                connection.execute(
                    "ALTER TABLE scheduled_tasks ADD COLUMN owner TEXT"
                )
            if "workspace" not in columns:
                # Legacy tasks have no trustworthy project identity. Keep
                # them for inspection, but never adopt them into this project.
                connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN workspace TEXT")
            if "lease_expires" not in columns:
                connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN lease_expires REAL")
            if "run_at_utc" not in columns:
                connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN run_at_utc REAL")
                for row in connection.execute('SELECT task_id, run_at FROM scheduled_tasks').fetchall():
                    try:
                        instant = datetime.fromisoformat(row['run_at'])
                        if instant.tzinfo is None:
                            raise ValueError('timezone required')
                    except ValueError:
                        connection.execute('UPDATE scheduled_tasks SET status = ?, last_error = ? WHERE task_id = ?',
                            (ScheduledTaskStatus.APPROVAL_REQUIRED.value,
                             'legacy task has no trustworthy timezone', row['task_id']))
                    else:
                        connection.execute('UPDATE scheduled_tasks SET run_at_utc = ? WHERE task_id = ?',
                                           (instant.timestamp(), row['task_id']))

    async def add(self, task: ScheduledTask) -> None:
        return await asyncio.to_thread(self._add_blocking, task)

    def _add_blocking(self, task: ScheduledTask) -> None:
        if task.run_at.tzinfo is None or task.run_at.utcoffset() is None:
            raise ValueError('scheduled task run_at must include a timezone')
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_tasks (
                    task_id, task_text, run_at, status, attempts,
                    max_attempts, idempotent, parent_task_id,
                    child_run_id, last_error, workspace, run_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.task_text,
                    task.run_at.astimezone(UTC).isoformat(),
                    task.status.value,
                    task.attempts,
                    task.max_attempts,
                    int(task.idempotent),
                    task.parent_task_id,
                    task.child_run_id,
                    task.last_error,
                    self.workspace,
                    task.run_at.timestamp(),
                ),
            )

    async def get(self, task_id: str) -> ScheduledTask:
        return await asyncio.to_thread(self._get_blocking, task_id)

    def _get_blocking(self, task_id: str) -> ScheduledTask:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id = ? AND workspace = ?",
                (task_id, self.workspace),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode(row)

    async def list(self) -> tuple[ScheduledTask, ...]:
        return await asyncio.to_thread(self._list_blocking)

    def _list_blocking(self) -> tuple[ScheduledTask, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE workspace = ? ORDER BY run_at_utc, task_id",
                (self.workspace,),
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        *,
        owner: str | None = None,
    ) -> tuple[ScheduledTask, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('scheduler clock must include a timezone')
        return await asyncio.to_thread(
            self._claim_due_blocking, now, limit, owner=owner
        )

    def _claim_due_blocking(
        self,
        now: datetime,
        limit: int,
        *,
        owner: str | None = None,
    ) -> tuple[ScheduledTask, ...]:
        # BEGIN IMMEDIATE takes SQLite's write lock before the SELECT, so two
        # processes polling the same database cannot both claim one task: the
        # loser blocks, then sees the row already marked running.
        # BEGIN IMMEDIATE takes SQLite's write lock before the SELECT, so two
        # processes polling the same database cannot both claim one task: the
        # loser blocks, then sees the row already marked running.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = ? AND run_at_utc <= ? AND workspace = ? AND attempts < max_attempts
                ORDER BY run_at_utc, task_id
                LIMIT ?
                """,
                (ScheduledTaskStatus.SCHEDULED.value, now.timestamp(), self.workspace, limit),
            ).fetchall()
            identifiers = [row["task_id"] for row in rows]
            claimed = []
            for task_id in identifiers:
                cursor = connection.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = ?, attempts = attempts + 1, owner = ?, lease_expires = ?
                    WHERE task_id = ? AND status = ?
                    """,
                    (
                        ScheduledTaskStatus.RUNNING.value,
                        owner,
                        now.timestamp() + 60 if owner else None,
                        task_id,
                        ScheduledTaskStatus.SCHEDULED.value,
                    ),
                )
                if cursor.rowcount:
                    claimed.append(task_id)
            connection.commit()
        rows = [row for row in rows if row["task_id"] in set(claimed)]
        return tuple(
            ScheduledTask(
                row["task_id"],
                row["task_text"],
                datetime.fromisoformat(row["run_at"]),
                ScheduledTaskStatus.RUNNING,
                row["attempts"] + 1,
                row["max_attempts"],
                bool(row["idempotent"]),
                row["parent_task_id"],
                row["child_run_id"],
                row["last_error"],
            )
            for row in rows
        )

    async def finish(
        self,
        task_id: str,
        *,
        child_run_id: str,
        succeeded: bool,
        error: str | None,
        owner: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        return await asyncio.to_thread(self._finish_blocking, task_id,
            child_run_id, succeeded, error, owner, attempt)

    def _finish_blocking(self, task_id, child_run_id, succeeded, error, owner, attempt):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE task_id = ? AND workspace = ?",
                (task_id, self.workspace)).fetchone()
            if (row is None or row['status'] != ScheduledTaskStatus.RUNNING.value
                    or row['owner'] != owner
                    or (attempt is not None and row['attempts'] != attempt)):
                return False
            if owner is not None and attempt is None:
                return False
            if succeeded:
                status = ScheduledTaskStatus.DONE
            elif row['idempotent'] and row['attempts'] < row['max_attempts']:
                status = ScheduledTaskStatus.SCHEDULED
            else:
                status = ScheduledTaskStatus.FAILED
            connection.execute(
                """
                UPDATE scheduled_tasks
                SET status = ?, child_run_id = ?, last_error = ?, owner = NULL, lease_expires = NULL
                WHERE task_id = ? AND workspace = ?
                """,
                (status.value, child_run_id, error, task_id, self.workspace),
            )
        return True

    async def renew_lease(self, task_id, *, owner, attempt, now) -> bool:
        def renew():
            with self._connect() as connection:
                cursor = connection.execute(
                    """UPDATE scheduled_tasks SET lease_expires = ?
                       WHERE task_id = ? AND workspace = ? AND status = ?
                         AND owner = ? AND attempts = ? AND lease_expires > ?""",
                    (now.timestamp() + 60, task_id, self.workspace,
                     ScheduledTaskStatus.RUNNING.value, owner, attempt, now.timestamp()))
                return bool(cursor.rowcount)
        return await asyncio.to_thread(renew)

    async def recover_running(
        self,
        *,
        completed_child_runs: tuple[str, ...],
        now: datetime | None = None,
    ) -> None:
        return await asyncio.to_thread(
            self._recover_running_blocking,
            completed_child_runs=completed_child_runs,
            now=now or datetime.now(UTC),
        )

    def _recover_running_blocking(
        self,
        *,
        completed_child_runs: tuple[str, ...],
        now: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE status = ? AND workspace = ?",
                (ScheduledTaskStatus.RUNNING.value, self.workspace),
            ).fetchall()
            for row in rows:
                if row['lease_expires'] is not None and row['lease_expires'] > now.timestamp():
                    continue
                if row["child_run_id"] in completed_child_runs:
                    status = ScheduledTaskStatus.DONE
                elif bool(row["idempotent"]) and row['attempts'] < row['max_attempts']:
                    status = ScheduledTaskStatus.SCHEDULED
                elif bool(row['idempotent']):
                    status = ScheduledTaskStatus.FAILED
                else:
                    status = ScheduledTaskStatus.APPROVAL_REQUIRED
                connection.execute(
                    "UPDATE scheduled_tasks SET status = ?, owner = NULL, lease_expires = NULL WHERE task_id = ?",
                    (status.value, row["task_id"]),
                )
            connection.commit()

    async def cancel(self, task_id: str) -> int:
        return await asyncio.to_thread(self._cancel_blocking, task_id)

    def _cancel_blocking(self, task_id: str) -> int:
        cancelled = 0
        pending = [task_id]
        visited = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                children = connection.execute(
                    "SELECT task_id FROM scheduled_tasks WHERE parent_task_id = ? AND workspace = ?",
                    (current, self.workspace),
                ).fetchall()
                pending.extend(row["task_id"] for row in children)
                cursor = connection.execute(
                    """
                    UPDATE scheduled_tasks SET status = ?
                    WHERE task_id = ? AND workspace = ? AND status NOT IN (?, ?, ?)
                    """,
                    (
                        ScheduledTaskStatus.CANCELLED.value,
                        current,
                        self.workspace,
                        ScheduledTaskStatus.DONE.value,
                        ScheduledTaskStatus.FAILED.value,
                        ScheduledTaskStatus.CANCELLED.value,
                    ),
                )
                cancelled += cursor.rowcount
            connection.commit()
        return cancelled

    async def unbound_count(self) -> int:
        def count():
            with self._connect() as connection:
                return connection.execute(
                    "SELECT COUNT(*) FROM scheduled_tasks WHERE workspace IS NULL"
                ).fetchone()[0]
        return await asyncio.to_thread(count)

    @staticmethod
    def _decode(row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            row["task_id"],
            row["task_text"],
            datetime.fromisoformat(row["run_at"]),
            ScheduledTaskStatus(row["status"]),
            row["attempts"],
            row["max_attempts"],
            bool(row["idempotent"]),
            row["parent_task_id"],
            row["child_run_id"],
            row["last_error"],
        )

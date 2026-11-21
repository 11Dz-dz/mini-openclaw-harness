import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from miniclaw.planning.engine import PlanEngine
from miniclaw.planning.types import (
    Plan,
    Task,
    TaskStatus,
    Todo,
    TodoStatus,
)

_TODO_STATUS = {
    TaskStatus.OPEN: TodoStatus.OPEN,
    TaskStatus.IN_PROGRESS: TodoStatus.IN_PROGRESS,
    TaskStatus.DONE: TodoStatus.DONE,
    TaskStatus.REPORTED_DONE: TodoStatus.IN_PROGRESS,
    TaskStatus.FAILED: TodoStatus.BLOCKED,
    TaskStatus.BLOCKED: TodoStatus.BLOCKED,
    TaskStatus.CANCELLED: TodoStatus.CANCELLED,
}


class PlanStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.engine = PlanEngine()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        return await asyncio.to_thread(self._initialize_blocking)

    def _initialize_blocking(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    PRIMARY KEY (plan_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS todos (
                    todo_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    PRIMARY KEY (plan_id, todo_id),
                    FOREIGN KEY (plan_id, task_id)
                        REFERENCES tasks(plan_id, task_id)
                );
                """
            )
            primary = [row['name'] for row in connection.execute('PRAGMA table_info(todos)')
                       if row['pk']]
            if primary == ['todo_id']:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('ALTER TABLE todos RENAME TO todos_legacy')
                connection.execute('''CREATE TABLE todos (
                    todo_id TEXT NOT NULL, plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL,
                    PRIMARY KEY (plan_id, todo_id), FOREIGN KEY (plan_id, task_id)
                    REFERENCES tasks(plan_id, task_id))''')
                connection.execute('INSERT INTO todos SELECT * FROM todos_legacy')
                connection.execute('DROP TABLE todos_legacy')
                connection.commit()
            evidence_exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'execution_evidence'").fetchone()
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS task_runs (
                    plan_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY(plan_id, task_id));
                CREATE TABLE IF NOT EXISTS execution_evidence (
                    evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL, output_sha256 TEXT NOT NULL, task_refs_json TEXT NOT NULL);
            ''')
            if not evidence_exists:
                connection.execute('UPDATE tasks SET status = ? WHERE status = ?',
                                   (TaskStatus.REPORTED_DONE.value, TaskStatus.DONE.value))
                connection.execute('UPDATE todos SET status = ? WHERE status = ?',
                                   (TodoStatus.IN_PROGRESS.value, TodoStatus.DONE.value))

    async def record_execution(self, call, output, context):
        # State bookkeeping is not evidence that the substantive task ran.
        meaningful = {'read_file', 'write_file', 'replace_text', 'run_command', 'fetch_url', 'web_search',
                      'git_status', 'git_diff', 'git_log', 'git_show'}
        if call.name not in meaningful and not call.name.startswith('mcp_'):
            return None
        if not isinstance(output, str):
            return None

        def record():
            evidence_id = 'execution:' + uuid4().hex
            with self._connect() as connection:
                rows = connection.execute('''SELECT t.plan_id, t.task_id, t.attempts FROM tasks t
                    JOIN task_runs r ON t.plan_id = r.plan_id AND t.task_id = r.task_id
                    WHERE r.run_id = ? AND t.status IN (?, ?)''',
                    (context.run_id, TaskStatus.IN_PROGRESS.value, TaskStatus.REPORTED_DONE.value)).fetchall()
                refs = [(r['plan_id'], r['task_id'], r['attempts']) for r in rows]
                connection.execute('INSERT INTO execution_evidence VALUES (?, ?, ?, ?, ?, ?)',
                    (evidence_id, context.run_id, call.id, call.name,
                     hashlib.sha256(output.encode()).hexdigest(), json.dumps(refs)))
            return evidence_id
        return await asyncio.to_thread(record)

    async def create(self, plan: Plan) -> None:
        return await asyncio.to_thread(self._create_blocking, plan)

    def _create_blocking(self, plan: Plan) -> None:
        self.engine.validate(plan)
        if any(task.status is TaskStatus.DONE for task in plan.tasks):
            raise ValueError('new plans cannot import unverified DONE tasks')
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO plans VALUES (?, ?)",
                (plan.plan_id, plan.goal),
            )
            for priority, task in enumerate(plan.tasks):
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan.plan_id,
                        task.task_id,
                        task.title,
                        json.dumps(task.depends_on),
                        task.status.value,
                        task.attempts,
                        task.max_attempts,
                        json.dumps(task.evidence),
                    ),
                )
                connection.execute(
                    "INSERT INTO todos VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.task_id,
                        plan.plan_id,
                        task.task_id,
                        task.title,
                        _TODO_STATUS[task.status].value,
                        priority,
                    ),
                )
            connection.commit()

    async def get(self, plan_id: str) -> Plan:
        return await asyncio.to_thread(self._get_blocking, plan_id)

    def _get_blocking(self, plan_id: str) -> Plan:
        with self._connect() as connection:
            plan_row = connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            task_rows = connection.execute(
                "SELECT * FROM tasks WHERE plan_id = ? ORDER BY rowid",
                (plan_id,),
            ).fetchall()
        if plan_row is None:
            raise KeyError(plan_id)
        return Plan(
            plan_row["plan_id"],
            plan_row["goal"],
            tuple(self._decode_task(row) for row in task_rows),
        )

    async def list_todos(self, plan_id: str) -> tuple[Todo, ...]:
        return await asyncio.to_thread(self._list_todos_blocking, plan_id)

    def _list_todos_blocking(self, plan_id: str) -> tuple[Todo, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM todos WHERE plan_id = ? ORDER BY priority",
                (plan_id,),
            ).fetchall()
        return tuple(
            Todo(
                row["todo_id"],
                row["plan_id"],
                row["task_id"],
                row["title"],
                TodoStatus(row["status"]),
                row["priority"],
            )
            for row in rows
        )

    async def list_tasks(self) -> tuple[tuple[str, Task], ...]:
        return await asyncio.to_thread(self._list_tasks_blocking)

    def _list_tasks_blocking(self) -> tuple[tuple[str, Task], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY rowid"
            ).fetchall()
        return tuple(
            (row["plan_id"], self._decode_task(row))
            for row in rows
        )

    async def transition(
        self,
        plan_id: str,
        task_id: str,
        target: TaskStatus,
        *,
        evidence: tuple[str, ...] = (),
        run_id: str = '',
    ) -> Task:
        return await asyncio.to_thread(self._transition_blocking, plan_id, task_id, target, evidence, run_id)

    def _transition_blocking(self, plan_id, task_id, target, evidence, run_id):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute('SELECT * FROM tasks WHERE plan_id = ?', (plan_id,)).fetchall()
            tasks = {row['task_id']: self._decode_task(row) for row in rows}
            if task_id not in tasks:
                raise KeyError(task_id)
            current = tasks[task_id]
            if (target in {TaskStatus.IN_PROGRESS, TaskStatus.DONE}
                    and any(tasks[dependency].status is not TaskStatus.DONE
                            for dependency in current.depends_on)):
                raise ValueError('task dependencies are not complete')
            updated = self.engine.transition(current, target, evidence=evidence)
            if target is TaskStatus.IN_PROGRESS:
                connection.execute('INSERT OR REPLACE INTO task_runs VALUES (?, ?, ?)',
                                   (plan_id, task_id, run_id))
            if target is TaskStatus.DONE:
                for reference in evidence:
                    proof = connection.execute('SELECT * FROM execution_evidence WHERE evidence_id = ?',
                                               (reference,)).fetchone()
                    if (proof is None or proof['run_id'] != run_id
                            or [plan_id, task_id, current.attempts] not in json.loads(proof['task_refs_json'])):
                        raise ValueError('completion evidence is not a successful execution for this task and run')
                updated = replace(updated, evidence=evidence)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, attempts = ?, evidence_json = ?
                WHERE plan_id = ? AND task_id = ?
                """,
                (
                    updated.status.value,
                    updated.attempts,
                    json.dumps(updated.evidence),
                    plan_id,
                    task_id,
                ),
            )
            connection.execute(
                """
                UPDATE todos SET status = ?
                WHERE plan_id = ? AND task_id = ?
                """,
                (_TODO_STATUS[updated.status].value, plan_id, task_id),
            )
            connection.commit()
        return updated

    @staticmethod
    def _decode_task(row: sqlite3.Row) -> Task:
        return Task(
            row["task_id"],
            row["title"],
            tuple(json.loads(row["depends_on_json"])),
            TaskStatus(row["status"]),
            row["attempts"],
            row["max_attempts"],
            tuple(json.loads(row["evidence_json"])),
        )

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from miniclaw.memory.search import memory_score, normalize
from miniclaw.memory.types import MemoryRecord


class MemorySourceRejected(ValueError):
    pass


class SqliteMemoryStore:
    def __init__(
        self,
        database_path: Path,
        source_directory: Path,
        *,
        secret_patterns: tuple[str, ...] = (),
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.database_path = database_path
        self.source_directory = source_directory
        self.secret_patterns = tuple(re.compile(pattern) for pattern in secret_patterns)
        self.secret_values = tuple(value for value in secret_values if value)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        return await asyncio.to_thread(self._initialize_blocking)

    def _initialize_blocking(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    supersedes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memories_normalized
                    ON memories(normalized_text);
                """
            )
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(memories)')}
            if 'provenance_status' not in columns:
                connection.execute("ALTER TABLE memories ADD COLUMN provenance_status TEXT NOT NULL DEFAULT 'legacy-unverified'")

    def _reject_secrets(self, text):
        if (any(value in text for value in self.secret_values)
                or any(pattern.search(text) for pattern in self.secret_patterns)
                or re.search(r'(?i)(password|api[_-]?key|token|secret)\s*[=:]\s*\S+', text)):
            raise MemorySourceRejected('memory or source contains credentials')

    def _verified_source(self, reference):
        match = re.fullmatch(r'file:([a-f0-9]{64})', reference)
        if not match:
            return False
        path = self.source_directory / f'{match[1]}.txt'
        if not path.is_file() or path.is_symlink():
            return False
        with path.open('rb') as file:
            data = file.read(65537)
        return len(data) <= 65536 and hashlib.sha256(data).hexdigest() == match[1]

    async def put(self, memory: MemoryRecord, *, confirmed: bool = False) -> None:
        return await asyncio.to_thread(self._put_blocking, memory, confirmed=confirmed)

    def _put_blocking(self, memory: MemoryRecord, *, confirmed: bool = False) -> None:
        if not memory.source_ref:
            raise ValueError("memory source_ref is required")
        self._reject_secrets(memory.text + '\n' + memory.source_ref + '\n' + ' '.join(memory.tags))
        verified = self._verified_source(memory.source_ref)
        if not verified and not confirmed:
            raise MemorySourceRejected('memory source is not verified; trusted imports require explicit confirmation')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if memory.supersedes is not None:
                previous = connection.execute('SELECT memory_id FROM memories WHERE memory_id = ?',
                                               (memory.supersedes,)).fetchone()
                replaced = connection.execute('SELECT memory_id FROM memories WHERE supersedes = ?',
                                               (memory.supersedes,)).fetchone()
                if previous is None or replaced is not None or memory.supersedes == memory.memory_id:
                    raise ValueError('supersedes must reference an active existing memory')
            connection.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory.memory_id,
                    memory.text,
                    normalize(memory.text),
                    memory.source_ref,
                    json.dumps(memory.tags, separators=(",", ":")),
                    memory.created_at,
                    memory.supersedes,
                    'content-verified' if verified else 'confirmed',
                ),
            )

    async def put_with_source(
        self,
        *,
        memory_id: str,
        text: str,
        source_content: str,
        tags: tuple[str, ...],
        created_at: str,
        supersedes: str | None = None,
    ) -> MemoryRecord:
        self._reject_secrets(source_content)
        data = source_content.encode()
        if len(data) > 65536:
            raise MemorySourceRejected('memory source exceeds 64 KiB')
        digest = hashlib.sha256(data).hexdigest()
        path = self.source_directory / f"{digest}.txt"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        source_ref = f"file:{digest}"
        memory = MemoryRecord(
            memory_id,
            text,
            source_ref,
            tags,
            created_at,
            supersedes,
        )
        await self.put(memory)
        return memory

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[MemoryRecord, ...]:
        return await asyncio.to_thread(self._search_blocking, query, limit=limit)

    def _search_blocking(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[MemoryRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT * FROM memories AS m
                WHERE provenance_status IN ('content-verified', 'confirmed')
                  AND NOT EXISTS (SELECT 1 FROM memories newer WHERE newer.supersedes = m.memory_id)
                """).fetchall()
        records = tuple(
            MemoryRecord(
                row["memory_id"],
                row["text"],
                row["source_ref"],
                tuple(json.loads(row["tags_json"])),
                row["created_at"],
                row["supersedes"],
            )
            for row in rows
            if row['provenance_status'] == 'confirmed' or self._verified_source(row['source_ref'])
        )
        scored = [
            (memory_score(record, query), record)
            for record in records
            if memory_score(record, query) > 0
        ]
        scored.sort(
            key=lambda item: (
                item[0],
                item[1].created_at,
                item[1].memory_id,
            ),
            reverse=True,
        )
        return tuple(record for _, record in scored[:limit])

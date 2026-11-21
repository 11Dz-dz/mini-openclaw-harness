import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from miniclaw.core.events import EventEnvelope
from miniclaw.observability.redaction import Redactor, audit_payload
from miniclaw.sessions.locking import file_lock
from miniclaw.sessions.serialization import (
    PersistenceError,
    decode_event,
    encode_event,
)


class JsonlEventStore:
    def __init__(self, path: Path, *, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self.redactor = Redactor(secrets)
        self.recovered_tails: list[Path] = []

        # Appending once per streamed delta must not re-parse the whole log,
        # so validation state is cached and rebuilt only when the file changed
        # underneath us (another store instance or process wrote to it).
        self._indexed_size = -1
        self._event_ids: set[str] = set()
        self._last_sequence: dict[str, int] = {}

    def _file_size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _sync_index(self) -> None:
        size = self._file_size()
        if size == self._indexed_size:
            return
        event_ids: set[str] = set()
        last_sequence: dict[str, int] = {}
        for event in self._read_blocking():
            event_ids.add(event.event_id)
            last_sequence[event.run_id] = event.sequence
        self._event_ids = event_ids
        self._last_sequence = last_sequence
        self._indexed_size = size

    async def append(self, envelope: EventEnvelope) -> None:
        await asyncio.to_thread(self._append_locked, envelope)

    def _append_locked(self, envelope):
        with file_lock(self.path.with_suffix('.lock')):
            self._append_blocking(envelope)

    def _append_blocking(self, envelope):
        record = encode_event(envelope)
        record['payload'] = audit_payload(record['payload_type'], record['payload'], self.redactor)
        envelope = decode_event(record)
        self._sync_index()
        if envelope.event_id in self._event_ids:
            previous = next(
                (
                    event
                    for event in self._read_blocking()
                    if event.event_id == envelope.event_id
                ),
                None,
            )
            if previous == envelope:
                return
            raise PersistenceError("conflicting duplicate event id")
        expected_sequence = self._last_sequence.get(envelope.run_id, 0) + 1
        if envelope.sequence != expected_sequence:
            raise PersistenceError("event sequence is not contiguous")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.path, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    encode_event(envelope),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            file.write("\n")
            file.flush()
        self._event_ids.add(envelope.event_id)
        self._last_sequence[envelope.run_id] = envelope.sequence
        self._indexed_size = self._file_size()

    async def load(self) -> tuple[EventEnvelope, ...]:
        return await asyncio.to_thread(self._load_locked)

    def _load_locked(self):
        with file_lock(self.path.with_suffix('.lock')):
            return self._read_blocking()

    def _read_blocking(self):
        if not self.path.exists():
            return ()
        data = self.path.read_bytes()
        lines = data.splitlines(keepends=True)
        events, offset = [], 0
        for index, line in enumerate(lines):
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError
            except (ValueError, TypeError, UnicodeDecodeError) as error:
                if index != len(lines) - 1 or line.endswith(b'\n'):
                    raise PersistenceError('invalid event log; complete record is corrupt') from error
                # Only a final unterminated record can be a torn append. The
                # valid prefix is already decoded; quarantine before truncating.
                quarantine = self.path.with_name(self.path.name + '.tail-' + uuid4().hex)
                descriptor = os.open(quarantine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as saved:
                    saved.write(line)
                    saved.flush()
                    os.fsync(saved.fileno())
                with self.path.open('r+b') as log:
                    log.truncate(offset)
                    log.flush()
                    os.fsync(log.fileno())
                self.recovered_tails.append(quarantine)
                self._indexed_size = -1
                break
            # A complete JSON object with an invalid schema is corruption,
            # even when its trailing newline is absent.
            events.append(decode_event(payload))
            offset += len(line)
        if data and events and len(events) == len(lines) and not data.endswith(b'\n'):
            # A complete final record with a missing newline is recoverable too.
            with self.path.open('ab') as log:
                log.write(b'\n')
                log.flush()
        return tuple(events)

import asyncio
from dataclasses import replace

import pytest

from miniclaw.agent.loop import RunStarted
from miniclaw.core.events import EventEnvelope
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.sessions.serialization import PersistenceError


async def test_torn_tail_is_quarantined_and_new_runs_can_append(tmp_path):
    path = tmp_path / 'events.jsonl'
    store = JsonlEventStore(path)
    first = EventEnvelope.create(run_id='a', session_id='s', sequence=1,
                                 payload=RunStarted('hello'))
    await store.append(first)
    prefix = path.read_bytes()
    with path.open('ab') as file:
        file.write(b'{"event_id":"incomplete')
    fresh = JsonlEventStore(path)
    await fresh.append(replace(first, event_id='second', run_id='b'))
    assert len(await fresh.load()) == 2
    assert path.read_bytes().startswith(prefix)
    assert fresh.recovered_tails[0].read_bytes() == b'{"event_id":"incomplete'


async def test_complete_corrupt_record_is_never_silently_skipped(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_bytes(b'not-json\npartial')
    with pytest.raises(PersistenceError, match='complete record'):
        await JsonlEventStore(path).load()
    assert path.read_bytes() == b'not-json\npartial'


async def test_valid_json_with_invalid_schema_is_not_mistaken_for_torn_write(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_bytes(b'{"version":999}')
    with pytest.raises(PersistenceError):
        await JsonlEventStore(path).load()
    assert path.read_bytes() == b'{"version":999}'


async def test_concurrent_instances_serialize_append_and_validate_under_lock(tmp_path):
    path = tmp_path / 'events.jsonl'
    events = [EventEnvelope.create(run_id=f'run-{i}', session_id='s', sequence=1,
                                   payload=RunStarted('hello')) for i in range(20)]
    await asyncio.gather(*(JsonlEventStore(path).append(e) for e in events))
    assert {e.event_id for e in await JsonlEventStore(path).load()} == {e.event_id for e in events}

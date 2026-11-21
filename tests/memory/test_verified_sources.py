from dataclasses import replace

import pytest

from miniclaw.memory.store import MemorySourceRejected, SqliteMemoryStore
from miniclaw.memory.types import MemoryRecord
from miniclaw.tools.builtin.memory import make_memory_tools
from miniclaw.tools.types import ToolContext


async def test_fabricated_references_are_rejected_by_default(tmp_path):
    store = SqliteMemoryStore(tmp_path / 'state.db', tmp_path / 'sources')
    await store.initialize()
    with pytest.raises(MemorySourceRejected, match='not verified'):
        await store.put(MemoryRecord('fake', 'claimed fact', 'invented:source', (), '2026-10-26'))
    remember = make_memory_tools(store)[0]
    with pytest.raises(ValueError, match='source_ref'):
        await remember.handler({'memory_id': 'fake', 'text': 'claim', 'source_ref': 'invented:source'},
                               ToolContext('run', 'session', tmp_path))


async def test_real_source_is_hashed_and_tampering_disables_retrieval(tmp_path):
    store = SqliteMemoryStore(tmp_path / 'state.db', tmp_path / 'sources')
    await store.initialize()
    memory = await store.put_with_source(memory_id='real', text='Python fact',
        source_content='Python fact from a real request', tags=(), created_at='2026-10-26')
    assert await store.search('Python') == (memory,)
    next(store.source_directory.glob('*.txt')).write_text('tampered source')
    assert await store.search('Python') == ()


async def test_superseded_memory_does_not_compete_with_correction(tmp_path):
    store = SqliteMemoryStore(tmp_path / 'state.db', tmp_path / 'sources')
    await store.initialize()
    original = await store.put_with_source(memory_id='old', text='Python 3.12',
        source_content='Python version was 3.12', tags=(), created_at='2026-10-25')
    corrected = replace(original, memory_id='new', text='Python 3.13', supersedes='old')
    await store.put(corrected)
    assert await store.search('Python') == (corrected,)


async def test_credentials_are_rejected_in_memory_text_and_source(tmp_path):
    store = SqliteMemoryStore(tmp_path / 'state.db', tmp_path / 'sources', secret_values=('known-secret',))
    await store.initialize()
    for text, source in [('safe', 'password=unknown-secret'), ('known-secret', 'safe source')]:
        with pytest.raises(MemorySourceRejected):
            await store.put_with_source(memory_id='secret', text=text, source_content=source,
                                        tags=(), created_at='2026-10-26')

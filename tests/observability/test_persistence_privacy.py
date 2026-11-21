import stat

from miniclaw.agent.loop import AgentLoop
from miniclaw.core.messages import ResponseCompleted, TextDelta, ToolCallDelta
from miniclaw.model.fake import FakeModel
from miniclaw.observability.trace import JsonlTraceSink
from miniclaw.sessions.checkpoints import FileCheckpointStore
from miniclaw.sessions.database import SessionRepository
from miniclaw.sessions.events import JsonlEventStore


async def test_unknown_split_credentials_do_not_reach_audit_streams(tmp_path):
    sink = JsonlTraceSink(tmp_path / 'trace')
    store = JsonlEventStore(tmp_path / 'events.jsonl')
    await AgentLoop(FakeModel((TextDelta('synthetic-'), TextDelta('credential'),
        ToolCallDelta(0, 'one', 'example', '{"token":"also-sensitive"}'),
        ResponseCompleted('stop'))), 'fake', event_store=store,
        trace_sinks=(sink,)).run('private prompt')
    for path in [tmp_path / 'events.jsonl', *sink.directory.glob('*.jsonl')]:
        text = path.read_text()
        assert 'private prompt' not in text
        assert 'synthetic-' not in text
        assert 'credential' not in text
        assert 'also-sensitive' not in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_checkpoint_keeps_recoverable_history_but_scrubs_credentials(tmp_path):
    repo = SessionRepository(tmp_path / 'state.db')
    await repo.initialize()
    store = FileCheckpointStore(tmp_path / 'checkpoints', repo, secrets=('known-value',))
    result = await AgentLoop(FakeModel((TextDelta('known-value'), ResponseCompleted('stop'))),
        'fake', checkpoint_store=store, session_repository=repo).run(
            'Keep this fact. password=unknown-value')
    restored = await store.latest_for_run(result.run_id)
    assert 'Keep this fact' in restored.messages[0].content[0].text
    for path in store.checkpoint_directory.glob('*.json'):
        assert 'known-value' not in path.read_text()
        assert 'unknown-value' not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

from pathlib import Path

import pytest

from miniclaw.permissions.types import ApprovalResolution
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.workspace.diff import changed_paths, snapshot_files
from miniclaw.workspace.session import WorkspaceSession


def make_session(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a').write_text('original')
    return source, WorkspaceSession.create(tmp_path / 'sandboxes', source)


def test_user_edits_are_not_agent_changes_and_cannot_be_overwritten(tmp_path):
    source, session = make_session(tmp_path)
    (source / 'a').write_text('user edit')
    assert changed_paths(session.baseline, snapshot_files(session.workspace)) == ()
    (session.workspace / 'a').write_text('agent edit')
    with pytest.raises(PermissionError, match='destination changed'):
        session.apply_artifacts(source, (Path('a'),))
    assert (source / 'a').read_text() == 'user edit'


@pytest.mark.parametrize('mutate_destination', [False, True])
async def test_changes_during_approval_abort_before_writing(tmp_path, mutate_destination):
    source, session = make_session(tmp_path)
    (session.workspace / 'a').write_text('reviewed')

    class Approver:
        async def resolve(self, request):
            assert 'sha256' in request.arguments_preview
            root = source if mutate_destination else session.workspace
            (root / 'a').write_text('concurrent edit')
            return ApprovalResolution(request.request_id, True, 'approved original')

    with pytest.raises(PermissionError, match='changed'):
        await session.apply_artifacts_approved(
            source, (Path('a'),), approval_provider=Approver(),
            event_store=JsonlEventStore(tmp_path / 'events.jsonl'))
    assert (source / 'a').read_text() == ('concurrent edit' if mutate_destination else 'original')


def test_successful_apply_advances_baseline(tmp_path):
    source, session = make_session(tmp_path)
    (session.workspace / 'a').write_text('agent edit')
    session.apply_artifacts(source, (Path('a'),))
    assert changed_paths(session.baseline, snapshot_files(session.workspace)) == ()

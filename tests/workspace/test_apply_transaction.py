import stat
from pathlib import Path

import pytest

from miniclaw.workspace.paths import WorkspaceEscape
from miniclaw.workspace.session import WorkspaceSession


def fixture(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('a', 'b'):
        (source / name).write_text('original ' + name)
    return source, WorkspaceSession.create(tmp_path / 'sandboxes', source)


def test_deletions_apply_and_success_advances_baseline(tmp_path):
    source, session = fixture(tmp_path)
    (session.workspace / 'a').unlink()
    assert session.apply_artifacts(source, (Path('a'),)) == (Path('a'),)
    assert not (source / 'a').exists()
    assert Path('a') not in session.baseline


def test_all_paths_are_preflighted_before_first_write(tmp_path):
    source, session = fixture(tmp_path)
    (session.workspace / 'a').write_text('changed')
    with pytest.raises(WorkspaceEscape):
        session.apply_artifacts(source, (Path('a'), Path('../escape')))
    assert (source / 'a').read_text() == 'original a'


def test_later_write_failure_rolls_back_earlier_write(tmp_path, monkeypatch):
    source, session = fixture(tmp_path)
    for name in ('a', 'b'):
        (session.workspace / name).write_text('changed ' + name)
    from miniclaw.workspace import apply
    original = apply.replace_file

    def fail_second(saved, target, mode):
        if target.name == 'b':
            raise OSError('injected write failure')
        return original(saved, target, mode)

    monkeypatch.setattr(apply, 'replace_file', fail_second)
    with pytest.raises(OSError, match='injected'):
        session.apply_artifacts(source, (Path('a'), Path('b')))
    assert (source / 'a').read_text() == 'original a'
    assert (source / 'b').read_text() == 'original b'
    assert any('rolled_back' in p.read_text() for p in session.artifacts.glob('apply-*/journal.json'))


def test_apply_preserves_executable_permissions(tmp_path):
    source, session = fixture(tmp_path)
    (session.workspace / 'a').chmod(0o755)
    session.apply_artifacts(source, (Path('a'),))
    assert stat.S_IMODE((source / 'a').stat().st_mode) == 0o755

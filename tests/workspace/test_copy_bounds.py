import pytest

from miniclaw.app import create_app
from miniclaw.config import MiniClawConfig
from miniclaw.core.errors import ApplicationConfigurationError
from miniclaw.model.fake import FakeModel
from miniclaw.workspace.session import WorkspaceSession


async def test_nested_data_directory_is_rejected_before_any_copy(tmp_path):
    with pytest.raises(ApplicationConfigurationError, match='must not overlap'):
        await create_app(MiniClawConfig(tmp_path / 'data', tmp_path), provider=FakeModel([]))
    assert not (tmp_path / 'data').exists()


def test_direct_session_api_cannot_recursively_copy_its_own_output(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        WorkspaceSession.create(tmp_path / 'sandboxes', tmp_path)
    assert not (tmp_path / 'sandboxes').exists()


def test_oversize_copy_fails_before_creating_output(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'large').write_bytes(b'x' * 100)
    monkeypatch.setattr('miniclaw.workspace.copying.MAX_COPY_BYTES', 50)
    with pytest.raises(ValueError, match='limit'):
        WorkspaceSession.create(tmp_path / 'sandboxes', source)
    assert not (tmp_path / 'sandboxes').exists()


def test_generated_dependency_trees_are_excluded(tmp_path):
    source = tmp_path / 'source'
    (source / '.venv').mkdir(parents=True)
    (source / '.venv' / 'large').write_text('dependency')
    (source / 'main.py').write_text('pass')
    session = WorkspaceSession.create(tmp_path / 'sandboxes', source)
    assert not (session.workspace / '.venv').exists()
    assert (session.workspace / 'main.py').read_text() == 'pass'

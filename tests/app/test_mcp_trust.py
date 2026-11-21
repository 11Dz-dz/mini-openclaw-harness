import json

from miniclaw.app import _start_mcp_servers
from miniclaw.permissions.types import ApprovalResolution
from miniclaw.tools.registry import ToolRegistry


async def test_no_approval_never_starts_project_program(tmp_path, monkeypatch):
    config = tmp_path / '.miniclaw' / 'mcp.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'servers': {'x': {'command': 'untrusted'}}}))

    def forbidden(*args, **kwargs):
        raise AssertionError('process must not even be constructed')

    monkeypatch.setattr('miniclaw.app.StdioMcpClient', forbidden)
    clients, status = await _start_mcp_servers(ToolRegistry(), tmp_path)
    assert clients == ()
    assert status[0]['status'] == 'untrusted'


async def test_mismatched_approval_is_refused_and_preview_binds_config(tmp_path):
    config = tmp_path / '.miniclaw' / 'mcp.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'servers': {'x': {
        'command': 'program', 'args': ['--path', 'output.txt'],
        'env': {'TOKEN': 'synthetic-secret'},
    }}}))

    class WrongApproval:
        async def resolve(self, request):
            preview = json.loads(request.arguments_preview)
            assert preview['command'] == 'program'
            assert preview['source'] == str(config)
            assert len(preview['sha256']) == 64
            assert 'synthetic-secret' not in request.arguments_preview
            return ApprovalResolution('different-request', True, 'wrong binding')

    clients, status = await _start_mcp_servers(
        ToolRegistry(), tmp_path, approval_provider=WrongApproval())
    assert not clients
    assert status[0]['status'] == 'untrusted'

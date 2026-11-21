import pytest

from miniclaw.mcp.isolated import DockerMcpClient, isolated_mcp_client
from miniclaw.mcp.types import McpError
from miniclaw.sandbox.docker import DockerSandbox


async def test_no_backend_refuses_host_execution(tmp_path):
    with pytest.raises(McpError, match='isolated stdio'):
        await isolated_mcp_client({}, tmp_path, None)


def test_mcp_mounts_only_workspace_copy_and_denies_host_capabilities(tmp_path):
    copy = tmp_path / 'copy'
    copy.mkdir()
    client = DockerMcpClient(dict(command='python', args=['server.py'],
                                 env={'TOKEN': 'fixture'}, timeout=1),
                             copy, DockerSandbox())
    args = client.args
    assert args[args.index('--network') + 1] == 'none'
    assert args[args.index('--workdir') + 1] == '/workspace'
    assert args[args.index('--mount') + 1] == (
        f'type=bind,src={copy.resolve()},dst=/workspace')
    assert args.count('--mount') == 1
    assert '--read-only' in args
    assert args[args.index('--entrypoint') + 1] == 'python'
    assert '--privileged' not in args
    assert 'fixture' not in client.env.values()

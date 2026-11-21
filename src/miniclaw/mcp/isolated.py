import asyncio
from pathlib import Path
from uuid import uuid4

from miniclaw.mcp.client import StdioMcpClient
from miniclaw.mcp.types import McpError
from miniclaw.sandbox.docker import DockerSandbox


class DockerMcpClient(StdioMcpClient):
    """The only MCP process launcher used by the application.

    The host project, credentials and Docker socket are never mounted. MCP
    servers must exist in the configured image or in the workspace copy.
    """

    def __init__(self, launch: dict, workspace: Path, sandbox: DockerSandbox):
        self.container_name = f"miniclaw-mcp-{uuid4().hex}"
        environment = tuple(
            value for key, item in sorted(launch['env'].items())
            for value in ('--env', f'{key}={item}')
        )
        super().__init__('docker', (
            'run', '--rm', '-i', '--name', self.container_name,
            '--pull', 'never', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
            '--mount', f'type=bind,src={workspace.resolve()},dst=/workspace',
            '--workdir', '/workspace', *environment,
            '--entrypoint', launch['command'], sandbox.image, *launch['args'],
        ), timeout=launch['timeout'])

    async def aclose(self):
        try:
            await super().aclose()
        finally:
            # Killing the CLI alone does not guarantee container termination.
            try:
                process = await asyncio.create_subprocess_exec(
                    'docker', 'rm', '-f', self.container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            except OSError:
                pass


async def isolated_mcp_client(launch, workspace, sandbox):
    if sandbox is None:
        raise McpError('MCP requires an isolated stdio backend')
    report = await sandbox.capabilities()
    if not (report.available and report.network_isolation
            and report.read_only_root):
        raise McpError(f'MCP isolation unavailable: {report.reason}')
    return DockerMcpClient(launch, workspace, sandbox)

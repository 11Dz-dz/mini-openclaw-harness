import asyncio

import pytest

from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.docker import DockerSandbox
from miniclaw.sandbox.router import SandboxCapabilityUnavailable, SandboxRouter
from miniclaw.sandbox.types import Command, SandboxPolicy
from miniclaw.tools.types import ToolCapabilities


async def test_docker_capability_has_public_reason() -> None:
    capabilities = await DockerSandbox().capabilities()
    assert capabilities.reason


async def test_router_never_silently_downgrades_strong() -> None:
    docker = DockerSandbox(
        image="image-that-is-not-present:miniclaw",
        allowed_images=("image-that-is-not-present:miniclaw",),
    )
    router = SandboxRouter(strong=docker)

    with pytest.raises(SandboxCapabilityUnavailable):
        await router.select(ToolCapabilities(subprocess=True), require_strong=True)


async def test_docker_executor_when_available(tmp_path) -> None:
    docker = DockerSandbox()
    capabilities = await docker.capabilities()
    if not capabilities.available:
        pytest.skip(f"strong sandbox unavailable: {capabilities.reason}")

    result = await docker.execute(
        Command(("python", "-c", "print('ok')"), tmp_path, {}),
        SandboxPolicy(
            tmp_path,
            "workspace-write",
            "deny",
            (),
            2.0,
            4096,
        ),
    )

    assert result.stdout.strip() == "ok"


async def test_docker_forwards_only_allowlisted_environment_and_preserves_empty_values(
    tmp_path, monkeypatch
) -> None:
    recorded = []
    monkeypatch.setenv("AUDIT_ALLOWED", "host value")
    monkeypatch.setenv("AUDIT_UNDECLARED", "synthetic host secret")

    async def available(self):
        return SandboxCapabilities(True, SandboxStrength.STRONG, True, True, True, "test")

    class Completed:
        returncode = 0

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b'ok')
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def launch(*argv, **kwargs):
        recorded.append(argv)
        return Completed()

    monkeypatch.setattr(DockerSandbox, "capabilities", available)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)

    await DockerSandbox(workspace_aliases=("/app",)).execute(
        Command(
            ("git", "status"), tmp_path,
            {"GIT_ALLOW_PROTOCOL": "", "AUDIT_ALLOWED": "explicit value", "AUDIT_UNDECLARED": "synthetic explicit secret"},
        ),
        SandboxPolicy(tmp_path, "read", "deny", ("GIT_ALLOW_PROTOCOL", "AUDIT_ALLOWED"), 2, 4096),
    )

    argv = recorded[0]
    for directory in ("/workspace", "/app"):
        assert f"type=bind,src={tmp_path.resolve()},dst={directory},readonly" in argv
    assert "GIT_ALLOW_PROTOCOL=" in argv
    assert "AUDIT_ALLOWED=explicit value" in argv
    assert not any("AUDIT_UNDECLARED" in argument or "secret" in argument for argument in argv)
    image_index = argv.index("python:3.12-slim")
    assert argv[image_index + 1:] == ("git", "status")

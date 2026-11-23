import asyncio
from types import SimpleNamespace

import pytest

from miniclaw.sandbox import docker as docker_module
from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.docker import DockerSandbox
from miniclaw.sandbox.router import SandboxCapabilityUnavailable, SandboxRouter
from miniclaw.sandbox.types import (
    Command,
    SandboxPolicy,
    SandboxResourceExceeded,
    SandboxTimeout,
)
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
        Command(
            ("python", "-c", "from pathlib import Path; Path('/tmp/probe').write_text('ok'); "
                             "print(Path('/tmp/probe').read_text())"),
            tmp_path,
            {},
        ),
        SandboxPolicy(
            tmp_path,
            "workspace-write",
            "deny",
            (),
            20.0,
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

    assert len(recorded) == 1
    argv = recorded[0]
    assert "/tmp:rw,nosuid,nodev,size=256m" in argv
    for directory in ("/workspace", "/app"):
        assert f"type=bind,src={tmp_path.resolve()},dst={directory},readonly" in argv
    assert "GIT_ALLOW_PROTOCOL=" in argv
    assert "AUDIT_ALLOWED=explicit value" in argv
    assert not any("AUDIT_UNDECLARED" in argument or "secret" in argument for argument in argv)
    image_index = argv.index("python:3.12-slim")
    assert argv[image_index + 1:] == ("git", "status")


async def test_docker_caches_available_capabilities_for_five_seconds(monkeypatch) -> None:
    now = 100.0
    statuses = iter((0, 0, 1, 0, 0))
    recorded = []

    async def launch(*argv, **kwargs):
        recorded.append(argv)
        return SimpleNamespace(returncode=next(statuses))

    async def collect(process, limit, timeout):
        return b"", b""

    monkeypatch.setattr(docker_module, "monotonic", lambda: now)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(docker_module, "communicate_bounded", collect)
    docker = DockerSandbox()

    assert (await docker.capabilities()).available
    now += 4.0
    assert (await docker.capabilities()).available
    assert len(recorded) == 2
    now += 1.0
    assert not (await docker.capabilities()).available
    assert (await docker.capabilities()).available
    assert len(recorded) == 5


@pytest.mark.parametrize("error", [SandboxTimeout, SandboxResourceExceeded])
async def test_docker_removes_container_after_reaping_interrupted_cli(tmp_path, monkeypatch, error) -> None:
    recorded = []

    async def available(self):
        return SandboxCapabilities(True, SandboxStrength.STRONG, True, True, True, "test")

    class Interrupted:
        returncode = None
        killed = False

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            if error is SandboxResourceExceeded:
                self.stdout.feed_data(b"too much output")

        def kill(self):
            self.killed = True

        async def wait(self):
            assert self.killed
            self.returncode = -9
            return self.returncode

    interrupted = Interrupted()

    async def cleanup_wait():
        return 0

    async def launch(*argv, **kwargs):
        recorded.append(argv)
        if argv[1] == "run":
            return interrupted
        assert interrupted.returncode == -9
        return SimpleNamespace(wait=cleanup_wait)

    monkeypatch.setattr(DockerSandbox, "capabilities", available)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    timeout = 0 if error is SandboxTimeout else 2
    with pytest.raises(error):
        await DockerSandbox().execute(
            Command(("python", "-V"), tmp_path, {}),
            SandboxPolicy(tmp_path, "read", "deny", (), timeout, 1),
        )
    argv = recorded[0]
    assert recorded[1:] == [("docker", "rm", "-f", argv[argv.index("--name") + 1])]

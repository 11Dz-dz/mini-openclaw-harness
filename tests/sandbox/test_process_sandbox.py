import asyncio
import sys
from pathlib import Path

import pytest

from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.sandbox.types import Command, SandboxCapabilityError, SandboxPolicy


@pytest.mark.parametrize("filesystem", ["none", "read", "workspace-write"])
async def test_unisolated_process_is_rejected_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem: str
) -> None:
    async def forbidden_launch(*args, **kwargs):
        pytest.fail("the unavailable process backend must never start a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_launch)

    with pytest.raises(SandboxCapabilityError, match="isolation"):
        await ProcessSandbox().execute(
            Command((sys.executable, "-c", "print('should not run')"), tmp_path, {}),
            SandboxPolicy(tmp_path, filesystem, "deny", ("PATH",), 2, 4096),
        )


async def test_requesting_network_access_cannot_enable_process_execution(
    tmp_path: Path,
) -> None:
    with pytest.raises(SandboxCapabilityError, match="isolation"):
        await ProcessSandbox().execute(
            Command((sys.executable, "-c", "print('should not run')"), tmp_path, {}),
            SandboxPolicy(tmp_path, "workspace-write", "allow", (), 2, 4096),
        )

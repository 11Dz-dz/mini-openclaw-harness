import sys
from pathlib import Path

import pytest

from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.sandbox.types import Command, SandboxCapabilityError, SandboxPolicy


async def assert_executor_contract(executor, tmp_path: Path) -> None:
    policy = SandboxPolicy(tmp_path, "workspace-write", "deny", ("PATH",), 2.0, 4096)
    result = await executor.execute(
        Command((sys.executable, "-c", "print('ok')"), tmp_path, {}),
        policy,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""


async def test_process_sandbox_contract(tmp_path: Path) -> None:
    executor = ProcessSandbox()
    assert not (await executor.capabilities()).available
    with pytest.raises(SandboxCapabilityError, match="isolation"):
        await assert_executor_contract(executor, tmp_path)

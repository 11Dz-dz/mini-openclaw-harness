import json
import sys
from pathlib import Path

import pytest

from miniclaw.app import create_app
from miniclaw.config import MiniClawConfig
from miniclaw.core.errors import ApplicationConfigurationError
from miniclaw.core.messages import ResponseCompleted, TextDelta, ToolCallDelta
from miniclaw.model.fake import FakeModel
from miniclaw.model.scripted import ScriptedModel
from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.sandbox.router import SandboxCapabilityUnavailable, SandboxRouter
from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResult,
)
from miniclaw.tools.types import ToolCapabilities, ToolContext

LOCAL_COMMANDS = {"run_command", "git_status", "git_log", "git_diff", "git_show"}


async def test_process_cannot_write_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "real-project-file"
    outside.write_text("user version")
    command = Command(
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(outside)!r}).write_text('overwritten')",
        ),
        workspace,
        {},
    )

    with pytest.raises(SandboxCapabilityError, match="isolation"):
        await ProcessSandbox().execute(
            command,
            SandboxPolicy(workspace, "workspace-write", "deny", (), 2, 4096),
        )

    assert outside.read_text() == "user version"


async def test_process_does_not_advertise_an_available_executor() -> None:
    report = await ProcessSandbox().capabilities()

    assert not report.available
    assert not report.network_isolation
    assert not report.read_only_root
    assert not report.resource_limits


class Backend:
    def __init__(self, *, available=True, isolated=True):
        self.report = SandboxCapabilities(
            available, SandboxStrength.STRONG, isolated, isolated, isolated,
            "test backend available" if available else "test backend unavailable",
        )

    async def capabilities(self):
        return self.report


async def test_router_rejects_an_available_but_unisolated_process() -> None:
    router = SandboxRouter(
        process=Backend(isolated=False), strong=Backend(available=False)
    )

    with pytest.raises(SandboxCapabilityUnavailable):
        await router.select(ToolCapabilities(subprocess=True), require_strong=False)


async def test_router_uses_isolated_backend_without_an_unsafe_fallback() -> None:
    strong = Backend()
    router = SandboxRouter(strong=strong)

    assert await router.select(
        ToolCapabilities(subprocess=True), require_strong=False
    ) is strong


@pytest.fixture
def no_docker(monkeypatch):
    async def unavailable(self):
        return Backend(available=False).report

    monkeypatch.setattr("miniclaw.app.DockerSandbox.capabilities", unavailable)


async def test_no_docker_keeps_file_tools_and_hides_local_commands(
    tmp_path: Path, no_docker
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes").write_text("original")
    provider = FakeModel((TextDelta("ok"), ResponseCompleted("stop")))
    app = await create_app(
        MiniClawConfig(tmp_path / "state", project), provider=provider
    )
    try:
        await app.run_prompt("hi")
        offered = {t["function"]["name"] for t in provider.requests[0].tools}
        assert {"read_file", "write_file"} <= offered
        assert not LOCAL_COMMANDS & offered
        context = ToolContext("test", "test", app.workspace_session.workspace)
        await app.tools.get("write_file").handler(
            {"path": "new-file", "content": "sandbox only"}, context
        )
        assert (app.workspace_session.workspace / "new-file").read_text() == "sandbox only"
        assert not (project / "new-file").exists()
        report = json.loads((await app.handle_command("/sandbox")).output)
        assert report["selected"] == "restricted"
        assert set(report["disabled_tools"]) == LOCAL_COMMANDS
        assert (project / "notes").read_text() == "original"
    finally:
        await app.aclose()


class NeverApprove:
    async def resolve(self, request):
        pytest.fail("an unavailable operation must be refused before approval")


@pytest.mark.parametrize("background", [False, True])
async def test_stale_or_preauthorized_command_cannot_bypass_restriction(
    tmp_path: Path, no_docker, background: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "outside"
    arguments = json.dumps({"argv": [
        sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe')",
    ]})
    provider = ScriptedModel((
        (ToolCallDelta(0, "stale", "run_command", arguments), ResponseCompleted("tool_calls")),
        (TextDelta("command unavailable"), ResponseCompleted("stop")),
    ))
    app = await create_app(
        MiniClawConfig(tmp_path / "state", project),
        provider=provider,
        approval_provider=NeverApprove(),
        unattended_approved_tools=("run_command",),
    )
    try:
        agent = app.scheduler_runtime.runner.agent if background else app.agent
        result = await agent.run("try a previously offered command")
        tool_result = result.messages[2].content[0]
        assert tool_result.is_error
        assert "unavailable" in tool_result.output
        assert not marker.exists()
    finally:
        await app.aclose()


async def test_strong_requirement_still_fails_before_state_creation(
    tmp_path: Path, no_docker
) -> None:
    with pytest.raises(ApplicationConfigurationError):
        await create_app(
            MiniClawConfig(
                tmp_path / "state", tmp_path / "project",
                require_strong_sandbox=True,
            ),
            provider=FakeModel(()),
        )
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "project").exists()


async def test_working_strong_backend_remains_available(tmp_path: Path, monkeypatch):
    calls = []

    async def capabilities(self):
        return Backend().report

    async def execute(self, command, policy):
        calls.append(command.argv)
        return SandboxResult(0, "isolated output", "", ())

    monkeypatch.setattr("miniclaw.app.DockerSandbox.capabilities", capabilities)
    monkeypatch.setattr("miniclaw.app.DockerSandbox.execute", execute)
    app = await create_app(
        MiniClawConfig(tmp_path / "state", tmp_path / "project"),
        provider=FakeModel(()),
    )
    try:
        offered = {s["function"]["name"] for s in app.tools.specs()}
        assert offered >= LOCAL_COMMANDS
        assert json.loads((await app.handle_command("/sandbox")).output)["selected"] == "strong"
        output = await app.tools.get("run_command").handler(
            {"argv": ["echo", "ok"]},
            ToolContext("test", "test", app.workspace_session.workspace),
        )
        assert calls == [("echo", "ok")]
        assert json.loads(output)["stdout"] == "isolated output"
    finally:
        await app.aclose()

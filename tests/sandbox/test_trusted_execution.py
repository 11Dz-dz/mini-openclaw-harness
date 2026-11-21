import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from miniclaw.app import create_app
from miniclaw.config import (
    ConfigDocument,
    MiniClawConfig,
    coerce_config_value,
    resolve_config,
)
from miniclaw.core.errors import ApplicationConfigurationError
from miniclaw.core.messages import ResponseCompleted, TextDelta, ToolCallDelta
from miniclaw.model.fake import FakeModel
from miniclaw.model.scripted import ScriptedModel
from miniclaw.permissions.types import ApprovalResolution
from miniclaw.sandbox.router import SandboxCapabilityUnavailable, SandboxRouter
from miniclaw.sandbox.trusted import TrustedProcessExecutor
from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResourceExceeded,
    SandboxTimeout,
)
from miniclaw.tools.types import ToolCapabilities, ToolContext

pytestmark = pytest.mark.skipif(os.name != "posix", reason="trusted executor requires POSIX")


def test_execution_mode_requires_explicit_opt_in():
    assert resolve_config(ConfigDocument({}), environ={}).execution_mode == "auto"
    assert resolve_config(ConfigDocument({}), environ={"MINICLAW_EXECUTION_MODE": "trusted"}).execution_mode == "trusted"
    with pytest.raises(ValueError, match="execution_mode"):
        coerce_config_value("execution_mode", "trust-me")
    with pytest.raises(ValueError, match="root"):
        coerce_config_value("trusted_user", "root")
    with pytest.raises(ValueError, match="requires execution_mode=auto"):
        resolve_config(ConfigDocument({"execution_mode": "trusted", "require_strong_sandbox": True}), environ={})


@pytest.fixture
def execution_user():
    import pwd

    return "nobody" if os.geteuid() == 0 else pwd.getpwuid(os.geteuid()).pw_name


@pytest.fixture
async def backend(tmp_path, execution_user):
    project = tmp_path / "project"
    project.mkdir()
    (project / "original").write_text("original")
    executor = TrustedProcessExecutor(execution_user)
    assert (await executor.capabilities()).available
    session = executor.create_session(tmp_path / "sessions", project)
    yield executor, session, project
    if os.geteuid() == 0:
        root = session.root.parent.resolve()
        assert root.parent == Path("/var/tmp") and root.name.startswith("miniclaw-trusted-")
        shutil.rmtree(root)


def policy(session, *, timeout=2, limit=4096):
    return SandboxPolicy(session.workspace, "host", "unrestricted", ("PATH",), timeout, limit)


async def test_trusted_reports_no_isolation_and_requires_explicit_policy(backend):
    executor, session, _ = backend
    report = await executor.capabilities()
    assert not report.read_only_root and not report.network_isolation and not report.resource_limits
    assert report.strength == "trusted" and "no filesystem" in report.reason
    with pytest.raises(SandboxCapabilityError, match="explicit host"):
        await executor.execute(Command(("true",), session.workspace, {}),
                               SandboxPolicy(session.workspace, "read", "deny", (), 2, 4096))
    with pytest.raises(SandboxCapabilityError, match="cwd"):
        await executor.execute(Command(("true",), session.root, {}), policy(session))


async def test_non_root_uid_filtered_env_copy_changes_and_protected_original(backend, monkeypatch):
    executor, session, project = backend
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-pass")
    code = (
        "import os,json; from pathlib import Path; "
        "Path('new').write_text('copy'); "
        "print(json.dumps({'uid':os.geteuid(),'groups':os.getgroups(),"
        "'secret':os.getenv('OPENAI_API_KEY'),'home':os.environ['HOME']}))"
    )
    result = await executor.execute(Command((sys.executable, "-c", code), session.workspace, {}), policy(session))
    assert result.exit_code == 0, result.stderr
    info = json.loads(result.stdout)
    assert info["uid"] == executor.uid != 0 and info["secret"] is None
    assert Path(info["home"]) == executor.home
    assert Path("new") in result.changed_paths and not (project / "new").exists()
    if os.geteuid() == 0:
        assert info["groups"] == []
        result = await executor.execute(Command((sys.executable, "-c",
            f"from pathlib import Path; Path({str(project / 'original')!r}).write_text('bad')"),
            session.workspace, {}), policy(session))
        assert result.exit_code != 0 and "PermissionError" in result.stderr
        assert (project / "original").read_text() == "original"


@pytest.mark.parametrize("failure", ["timeout", "output", "cancel"])
async def test_failure_stops_shell_children(backend, failure):
    executor, session, _ = backend
    # A delayed child would write after the parent's timeout/cancellation unless
    # the whole ordinary process group is stopped. It deliberately inherits pipes.
    child = "import time; from pathlib import Path; time.sleep(0.8); Path('leaked').write_text('bad')"
    code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        + ("print('x'*100000,flush=True); " if failure == "output" else "")
        + "time.sleep(30)"
    )
    task = asyncio.create_task(executor.execute(
        Command((sys.executable, "-c", code), session.workspace, {}), policy(session, timeout=0.3)))
    if failure == "cancel":
        await asyncio.sleep(0.15)
        task.cancel()
    expected = {"timeout": SandboxTimeout, "output": SandboxResourceExceeded,
                "cancel": asyncio.CancelledError}[failure]
    with pytest.raises(expected):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(0.9)
    assert not (session.workspace / "leaked").exists()


async def test_success_stops_background_children(backend):
    executor, session, _ = backend
    child = "import time; from pathlib import Path; time.sleep(0.8); Path('leaked').write_text('bad')"
    code = ("import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)")
    result = await executor.execute(Command((sys.executable, "-c", code), session.workspace, {}), policy(session))
    assert result.exit_code == 0
    await asyncio.sleep(0.9)
    assert not (session.workspace / "leaked").exists()


class Approval:
    def __init__(self, allow):
        self.allow = allow
        self.requests = []

    async def resolve(self, request):
        self.requests.append(request)
        return ApprovalResolution(request.request_id, self.allow, "test")


@pytest.mark.parametrize("allow", [False, True])
async def test_app_shell_approval_and_copy_apply(tmp_path, execution_user, allow):
    project = tmp_path / "project"
    project.mkdir()
    provider = ScriptedModel((
        (ToolCallDelta(0, "cmd", "run_command", json.dumps({"argv": [sys.executable, "-c",
            "from pathlib import Path; Path('result').write_text('approved')"]})), ResponseCompleted("tool_calls")),
        (TextDelta("done"), ResponseCompleted("stop")),
    ))
    approval = Approval(allow)
    app = await create_app(MiniClawConfig(tmp_path / "state", project, execution_mode="trusted", trusted_user=execution_user),
                           provider=provider, approval_provider=approval)
    root = app.workspace_session.root.parent
    try:
        result = await app.run_prompt("write a result")
        assert len(approval.requests) == 1 and approval.requests[0].tool_name == "run_command"
        assert (app.workspace_session.workspace / "result").exists() is allow
        assert not (project / "result").exists()
        if allow:
            assert '"exit_code":0' in result.messages[2].content[0].output
            assert "result" in (await app.handle_command("/changes")).output
            await app.handle_command("/apply result")
            assert (project / "result").read_text() == "approved"
        status = json.loads((await app.handle_command("/sandbox")).output)
        assert status["selected"] == "trusted" and status["warning"]
        assert not status["trusted"]["network_isolation"]
    finally:
        await app.aclose()
        if os.geteuid() == 0:
            shutil.rmtree(root)


async def test_git_approval_mcp_disabled_and_file_tool_ownership(tmp_path, execution_user):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".miniclaw").mkdir()
    (project / ".miniclaw/mcp.json").write_text(json.dumps({"servers": {"test": {"command": "do-not-run"}}}))
    approval = Approval(False)
    provider = ScriptedModel((
        (ToolCallDelta(0, "git", "git_status", "{}"), ResponseCompleted("tool_calls")),
        (TextDelta("denied"), ResponseCompleted("stop")),
    ))
    app = await create_app(MiniClawConfig(tmp_path / "state", project, execution_mode="trusted", trusted_user=execution_user),
                           provider=provider, approval_provider=approval)
    root = app.workspace_session.root.parent
    try:
        assert not approval.requests, "MCP must not request trust or start in trusted mode"
        assert app.mcp_status[0]["status"] == "unavailable"
        await app.run_prompt("inspect git")
        assert len(approval.requests) == 1 and approval.requests[0].tool_name == "git_status"
        ctx = ToolContext("test", "test", app.workspace_session.workspace)
        await app.tools.get("write_file").handler({"path": "nested/file", "content": "initial"}, ctx)
        output = await app.tools.get("run_command").handler({"argv": [sys.executable, "-c",
            "from pathlib import Path; Path('nested/file').write_text('command')"]}, ctx)
        assert json.loads(output)["exit_code"] == 0, output
        assert (ctx.workspace / "nested/file").read_text() == "command"
        # A scheduled run must deny trusted Git rather than silently allowing it.
        provider._turns.extend(provider._turns[:2])
        await app.scheduler_runtime.runner.agent.run("inspect git")
        assert len(approval.requests) == 1
    finally:
        await app.aclose()
        if os.geteuid() == 0:
            shutil.rmtree(root)


async def test_unavailable_trusted_backend_never_falls_back(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        pytest.fail("strong backend must not be probed as a trusted fallback")

    monkeypatch.setattr("miniclaw.app.DockerSandbox.capabilities", fail)
    with pytest.raises(ApplicationConfigurationError, match="does not exist"):
        await create_app(MiniClawConfig(tmp_path / "state", tmp_path / "project",
            execution_mode="trusted", trusted_user="missing_miniclaw_test_account"), provider=FakeModel(()))
    assert not (tmp_path / "state").exists()


async def test_router_does_not_choose_trusted_without_opt_in():
    executor = TrustedProcessExecutor()
    router = SandboxRouter(trusted=executor)
    with pytest.raises(SandboxCapabilityUnavailable):
        await router.select(ToolCapabilities(subprocess=True), require_strong=False)
    assert router.selected == "restricted"


async def test_git_inspection_tools_execute_in_non_root_copy(backend):
    from miniclaw.tools.builtin.git import make_git_tools

    executor, session, _ = backend
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "add", "original"),
        ("git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-q", "-m", "trusted fixture"),
    ):
        result = await executor.execute(Command(argv, session.workspace, {}), policy(session))
        assert result.exit_code == 0, result.stderr
    (session.workspace / "original").write_text("modified\n")
    tools = {tool.spec.name: tool for tool in make_git_tools(executor, trusted=True)}
    ctx = ToolContext("git-test", "git-test", session.workspace)
    assert "original" in await tools["git_status"].handler({}, ctx)
    assert "trusted fixture" in await tools["git_log"].handler({}, ctx)
    assert "+modified" in await tools["git_diff"].handler({}, ctx)
    assert "trusted fixture" in await tools["git_show"].handler({"revision": "HEAD"}, ctx)


def test_cli_explicit_trusted_selection_reaches_runtime(tmp_path, execution_user):
    from miniclaw.cli.repl import main

    path = tmp_path / "config.toml"
    path.write_text('[miniclaw]\nmodel = "demo"\n')
    path.chmod(0o600)
    received = []
    status = main(["--config", str(path), "--execution-mode", "trusted",
                   "--trusted-user", execution_user], environ={},
                  application_runner=lambda config, secrets: received.append(config) or 0)
    assert status == 0
    assert received[0].execution_mode == "trusted"
    assert received[0].trusted_user == execution_user

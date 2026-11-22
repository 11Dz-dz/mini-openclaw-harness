import contextlib
import os
import stat
import sys
from pathlib import Path

import pytest

from miniclaw.agent.loop import AgentLoop
from miniclaw.agent.state import RunStatus
from miniclaw.core.messages import ResponseCompleted, TextDelta, ToolCall, ToolCallDelta
from miniclaw.model.scripted import ScriptedModel
from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.builtin.shell import make_run_command_tool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import ToolContext
from miniclaw.workspace.session import WorkspaceSession


async def test_replace_text_changes_only_the_copy_and_preserves_file_format(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = "标题\r\nvalue = 1\r\nfooter".encode()
    (source / "sample.py").write_bytes(original)
    session = WorkspaceSession.create(tmp_path / "sandboxes", source)
    target = session.workspace / "sample.py"
    target.chmod(0o750)
    initial_mode = stat.S_IMODE(target.stat().st_mode)
    registry = ToolRegistry()
    tool = next(tool for tool in make_file_tools() if tool.spec.name == "replace_text")
    registry.register(tool)
    context = ToolContext("run", "session", session.workspace)

    result = await ToolExecutor(registry).execute(
        ToolCall(
            "edit", "replace_text",
            {"path": "sample.py", "old_text": "value = 1\r\n", "new_text": ""},
        ),
        context,
    )
    assert not result.is_error
    assert tool.spec.capabilities.filesystem == "workspace-write"
    assert tool.spec.capabilities.side_effects and not tool.spec.capabilities.idempotent
    assert target.read_bytes() == "标题\r\nfooter".encode()
    assert stat.S_IMODE(target.stat().st_mode) == initial_mode
    assert (source / "sample.py").read_bytes() == original


@pytest.mark.parametrize(
    ("relative", "old_text", "new_text", "error"),
    [
        ("sample.txt", "absent", "replacement", "was not found"),
        ("sample.txt", "aa", "replacement", "occurs more than once"),
        ("sample.txt", "abc", "abc", "different from new_text"),
        ("missing.txt", "abc", "replacement", "existing regular file"),
        ("../outside.txt", "abc", "replacement", "escapes the workspace"),
    ],
)
async def test_replace_text_leaves_rejected_edits_unchanged(
    tmp_path: Path, relative: str, old_text: str, new_text: str, error: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample = workspace / "sample.txt"
    sample.write_bytes(b"aaa abc")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"abc")
    registry = ToolRegistry()
    for tool in make_file_tools():
        registry.register(tool)

    result = await ToolExecutor(registry).execute(
        ToolCall("edit", "replace_text", {"path": relative, "old_text": old_text, "new_text": new_text}),
        ToolContext("run", "session", workspace),
    )

    assert result.is_error and error in result.output
    assert sample.read_bytes() == b"aaa abc"
    assert outside.read_bytes() == b"abc"
    assert list(workspace.iterdir()) == [sample]


async def test_replace_text_preserves_a_concurrent_change(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"original")
    registry = ToolRegistry()
    for tool in make_file_tools():
        registry.register(tool)
    fsync = os.fsync

    def write_concurrent_change(descriptor: int) -> None:
        fsync(descriptor)
        target.write_bytes(b"concurrent change")

    monkeypatch.setattr("miniclaw.tools.builtin.files.os.fsync", write_concurrent_change)
    result = await ToolExecutor(registry).execute(
        ToolCall("edit", "replace_text", {"path": "sample.txt", "old_text": "original", "new_text": "edit"}),
        ToolContext("run", "session", tmp_path),
    )
    assert result.is_error and "file changed" in result.output
    assert target.read_bytes() == b"concurrent change"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("path", ["/app/../outside.txt", "/app2/outside.txt", "/outside.txt"])
async def test_container_alias_only_resolves_paths_within_the_mounted_workspace(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("inside")
    (tmp_path / "outside.txt").write_text("outside")
    registry = ToolRegistry()
    registry.register(make_file_tools(mounts=("/app",))[0])
    executor = ToolExecutor(registry)
    context = ToolContext("run", "session", workspace)
    inside = await executor.execute(ToolCall("read", "read_file", {"path": "/app/sample.txt"}), context)
    outside = await executor.execute(ToolCall("outside", "read_file", {"path": path}), context)
    assert inside.output == "1: inside" and not inside.is_error
    assert outside.is_error and outside.output != "outside"


def test_container_alias_rejects_symlinks_outside_the_workspace(tmp_path: Path) -> None:
    from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.touch()
    try:
        (workspace / "linked.txt").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(WorkspaceEscape, match="escapes the workspace"):
        resolve_workspace_path(workspace, "/app/linked.txt", mounts=("/app",))


async def test_read_file_pages_long_utf8_files_without_splitting_a_line(tmp_path: Path) -> None:
    lines = [f"{i:03}: {'中' * 100}\r\n" for i in range(1, 301)]
    (tmp_path / "large.txt").write_bytes("".join(lines).encode())
    registry = ToolRegistry()
    registry.register(make_file_tools()[0])
    executor = ToolExecutor(registry)
    context = ToolContext("run", "session", tmp_path)

    first = await executor.execute(ToolCall("first", "read_file", {"path": "large.txt"}), context)
    assert not first.is_error
    assert first.output == "".join(f"{i}: {line}" for i, line in enumerate(lines[:39], 1)) + (
        "[Read lines 1-39 of 300.] Continue with start_line=40.")

    second = await executor.execute(
        ToolCall("second", "read_file", {"path": "large.txt", "start_line": 27, "max_lines": 2}),
        context,
    )
    assert not second.is_error
    assert second.output == "".join(f"{i}: {line}" for i, line in enumerate(lines[26:28], 27)) + (
        "[Read lines 27-28 of 300.] Continue with start_line=29.")


async def test_read_file_preserves_small_results_and_rejects_invalid_ranges(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_bytes(b"small")
    registry = ToolRegistry()
    registry.register(make_file_tools()[0])
    executor = ToolExecutor(registry)
    context = ToolContext("run", "session", tmp_path)

    small = await executor.execute(ToolCall("one", "read_file", {"path": "small.txt"}), context)
    invalid = await executor.execute(
        ToolCall("two", "read_file", {"path": "small.txt", "start_line": 0}), context,
    )
    assert not small.is_error and small.output == "1: small"
    assert invalid.is_error


async def test_search_text_returns_literal_matches_and_stays_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("other\na.b\naxb\na.b again\n")
    (workspace / "binary.dat").write_bytes(b"\x00a.b")
    outside = tmp_path / "outside.txt"
    outside.write_text("a.b outside")
    with contextlib.suppress(OSError):
        (workspace / "link.txt").symlink_to(outside)
    registry = ToolRegistry()
    for tool in make_file_tools(mounts=("/app",)):
        registry.register(tool)
    executor = ToolExecutor(registry)
    context = ToolContext("run", "session", workspace)
    result = await executor.execute(ToolCall("one", "search_text", {"pattern": "a.b", "path": "/app"}), context)
    limited = await executor.execute(ToolCall("two", "search_text", {"pattern": "a.b", "max_results": 1}), context)
    escaped = await executor.execute(ToolCall("three", "search_text", {"pattern": "a.b", "path": "../outside.txt"}), context)
    assert not result.is_error and result.output == "main.py:2:a.b\nmain.py:4:a.b again"
    assert limited.output.startswith("main.py:2:a.b\n[Results truncated")
    assert escaped.is_error


async def test_agent_changes_only_sandbox_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "workspace-secret-value"
    monkeypatch.setenv("SECRET_TOKEN", secret)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("original")
    session = WorkspaceSession.create(tmp_path / "sandboxes", source)
    results = []
    registry = ToolRegistry()
    for tool in make_file_tools():
        registry.register(tool)
    registry.register(
        make_run_command_tool(
            ProcessSandbox(),
            allowed_environment=("PATH",),
            result_sink=results.append,
        )
    )
    provider = ScriptedModel(
        (
            (
                ToolCallDelta(
                    0,
                    "write-1",
                    "write_file",
                    '{"path":"notes.txt","content":"sandbox only"}',
                ),
                ResponseCompleted("tool_calls"),
            ),
            (
                ToolCallDelta(
                    0,
                    "run-1",
                    "run_command",
                    (
                        '{"argv":'
                        f'["{sys.executable}","-c",'
                        '"from pathlib import Path; '
                        "Path('command.txt').write_text('ok')\"]}"
                    ),
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("done"), ResponseCompleted("stop")),
        )
    )

    result = await AgentLoop(
        provider,
        "fake",
        tools=registry,
        workspace=session.workspace,
    ).run("write files safely")

    assert result.status is RunStatus.COMPLETED
    assert (session.workspace / "notes.txt").read_text() == "sandbox only"
    assert not (session.workspace / "command.txt").exists()
    assert not (source / "notes.txt").exists()
    assert not (source / "command.txt").exists()
    assert not results
    assert result.messages[4].content[0].is_error
    assert "SandboxCapabilityError" in result.messages[4].content[0].output
    assert secret not in repr(result)
    assert secret not in repr(results)


async def test_file_tool_returns_public_error_for_escape(tmp_path: Path) -> None:
    registry = ToolRegistry()
    for tool in make_file_tools():
        registry.register(tool)
    provider = ScriptedModel(
        (
            (
                ToolCallDelta(
                    0,
                    "write-escape",
                    "write_file",
                    '{"path":"../escape","content":"blocked"}',
                ),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("blocked"), ResponseCompleted("stop")),
        )
    )

    result = await AgentLoop(
        provider,
        "fake",
        tools=registry,
        workspace=tmp_path,
    ).run("escape")

    tool_result = result.messages[2].content[0]
    assert tool_result.is_error is True
    # The model is told why so it can correct itself, but the resolved
    # filesystem path never reaches it.
    assert "escapes the workspace" in tool_result.output
    assert str(tmp_path) not in tool_result.output
    assert not (tmp_path.parent / "escape").exists()

import asyncio
import subprocess
from pathlib import Path

import pytest

from miniclaw.core.messages import ToolCall
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.types import PermissionDecision
from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.sandbox.types import SandboxResult
from miniclaw.tools.builtin.git import make_git_tools
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import ToolContext
from miniclaw.workspace.paths import WorkspaceEscape


def _git(repository: Path, *argv: str) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("first\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "initial commit")
    return tmp_path


class FixtureGitExecutor:
    """Run only the controlled Git fixtures in this module, not user projects.

    Production ProcessSandbox intentionally cannot execute commands. These
    tool tests exercise real Git argv/output on fresh test-owned repositories
    without pretending that a subprocess is an isolation boundary.
    """

    async def execute(self, command, policy):
        assert command.argv[:2] == ("git", "--no-pager")
        assert any(item in {"status", "log", "diff", "show", "config"} for item in command.argv[2:])
        assert command.cwd == policy.workspace
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **command.environment},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return SandboxResult(process.returncode, stdout.decode(), stderr.decode(), ())


def _tools() -> dict:
    return {
        tool.spec.name: tool for tool in make_git_tools(FixtureGitExecutor())
    }


def _context(repository: Path) -> ToolContext:
    return ToolContext("run-1", "session-1", repository)


async def test_status_reports_a_dirty_working_tree(repository: Path) -> None:
    (repository / "README.md").write_text("changed\n")

    output = await _tools()["git_status"].handler({}, _context(repository))

    assert "## main" in output
    assert "README.md" in output


async def test_log_lists_commits(repository: Path) -> None:
    output = await _tools()["git_log"].handler(
        {"limit": 5}, _context(repository)
    )

    assert "initial commit" in output


async def test_diff_shows_unstaged_changes(repository: Path) -> None:
    (repository / "README.md").write_text("second\n")

    output = await _tools()["git_diff"].handler({}, _context(repository))

    assert "-first" in output
    assert "+second" in output


async def test_show_renders_one_commit(repository: Path) -> None:
    output = await _tools()["git_show"].handler(
        {"revision": "HEAD"}, _context(repository)
    )

    assert "initial commit" in output
    assert "README.md" in output


async def test_revision_cannot_smuggle_an_option(repository: Path) -> None:
    with pytest.raises(ValueError, match="revision"):
        await _tools()["git_show"].handler(
            {"revision": "--upload-pack=touch /tmp/pwned"},
            _context(repository),
        )


async def test_path_cannot_escape_the_workspace(repository: Path) -> None:
    with pytest.raises(WorkspaceEscape):
        await _tools()["git_log"].handler(
            {"path": "../../../etc/passwd"}, _context(repository)
        )


async def test_limit_is_bounded(repository: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        await _tools()["git_log"].handler(
            {"limit": 100_000}, _context(repository)
        )


async def test_non_repository_reports_a_readable_failure(tmp_path: Path) -> None:
    output = await _tools()["git_status"].handler({}, _context(tmp_path))

    assert output.startswith("git status failed:")


def test_read_only_git_does_not_require_approval() -> None:
    for tool in make_git_tools(ProcessSandbox()):
        result = DefaultPermissionPolicy().evaluate(
            tool_name=tool.spec.name,
            capabilities=tool.spec.capabilities,
            arguments={},
        )
        assert result.decision is PermissionDecision.ALLOW, tool.spec.name
        assert result.reason == "read-only inspection tool"


def test_write_capable_shell_still_requires_approval() -> None:
    from miniclaw.sandbox.process import ProcessSandbox as Sandbox
    from miniclaw.tools.builtin.shell import make_run_command_tool

    tool = make_run_command_tool(Sandbox())
    result = DefaultPermissionPolicy().evaluate(
        tool_name=tool.spec.name,
        capabilities=tool.spec.capabilities,
        arguments={"argv": ["echo", "hi"]},
    )

    assert result.decision is PermissionDecision.ASK


def _helper(repository: Path, name: str, body: str = "exit 0") -> tuple[Path, Path]:
    marker = repository.parent / f"{repository.name}-{name}.marker"
    script = repository / ".git" / f"audit-{name}.sh"
    script.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\n{body}\n")
    script.chmod(0o700)
    return script, marker


async def test_diff_never_runs_repository_external_diff(repository: Path) -> None:
    script, marker = _helper(repository, "external-diff")
    _git(repository, "config", "diff.external", str(script))
    (repository / "README.md").write_text("second\n")

    output = await _tools()["git_diff"].handler({}, _context(repository))

    assert not marker.exists(), "read-only diff executed diff.external"
    assert "-first" in output and "+second" in output


async def test_diff_never_runs_attribute_selected_external_driver(repository: Path) -> None:
    script, marker = _helper(repository, "attribute-driver")
    (repository / ".gitattributes").write_text("README.md diff=audit\n")
    _git(repository, "config", "diff.audit.command", str(script))
    (repository / "README.md").write_text("second\n")

    output = await _tools()["git_diff"].handler({}, _context(repository))

    assert not marker.exists(), "gitattributes selected an executable diff driver"
    assert "-first" in output and "+second" in output


@pytest.mark.parametrize("tool_name", ["git_diff", "git_show"])
async def test_patch_tools_never_run_textconv(repository: Path, tool_name: str) -> None:
    script, marker = _helper(repository, "textconv", 'cat "$1"')
    (repository / ".gitattributes").write_text("README.md diff=audit\n")
    _git(repository, "config", "diff.audit.textconv", str(script))
    (repository / "README.md").write_text("second\n")
    arguments = {"revision": "HEAD"} if tool_name == "git_show" else {}

    output = await _tools()[tool_name].handler(arguments, _context(repository))

    assert not marker.exists(), f"{tool_name} ran the repository textconv command"
    assert "first" in output


@pytest.mark.parametrize("included", [False, True])
async def test_status_never_runs_fsmonitor(repository: Path, included: bool) -> None:
    script, marker = _helper(repository, "fsmonitor", "printf 'token\\0'")
    if included:
        config = repository / ".git" / "included.cfg"
        config.write_text(f"[core]\n\tfsmonitor = {script}\n")
        _git(repository, "config", "include.path", str(config))
    else:
        _git(repository, "config", "core.fsmonitor", str(script))
    (repository / "README.md").write_text("changed\n")

    output = await _tools()["git_status"].handler({}, _context(repository))

    assert not marker.exists(), "git status ran the fsmonitor hook"
    assert "README.md" in output


async def test_missing_blob_never_starts_a_remote_helper(repository: Path) -> None:
    script, marker = _helper(repository, "remote-helper")
    object_id = (await asyncio.to_thread(
        subprocess.check_output,
        ["git", "rev-parse", "HEAD:README.md"], cwd=repository, text=True,
    )).strip()
    _git(repository, "config", "core.repositoryformatversion", "1")
    _git(repository, "config", "extensions.partialClone", "origin")
    _git(repository, "config", "remote.origin.promisor", "true")
    _git(repository, "config", "remote.origin.url", f"ext::{script}")
    _git(repository, "config", "protocol.ext.allow", "always")
    (repository / ".git" / "objects" / object_id[:2] / object_id[2:]).unlink()

    output = await _tools()["git_show"].handler({"revision": "HEAD"}, _context(repository))

    assert not marker.exists(), "inspection of a missing object launched a remote helper"
    assert output.startswith("git show failed:")


async def test_status_does_not_refresh_index_on_disk(repository: Path) -> None:
    index = repository / ".git" / "index"
    before = index.read_bytes()
    (repository / "README.md").write_text("changed\n")

    output = await _tools()["git_status"].handler({}, _context(repository))

    assert "README.md" in output
    assert index.read_bytes() == before


@pytest.mark.parametrize("tool_name", ["git_status", "git_diff"])
async def test_worktree_inspection_never_runs_clean_filter(repository: Path, tool_name: str) -> None:
    script, marker = _helper(repository, "clean-filter", "cat")
    (repository / ".gitattributes").write_text("README.md filter=audit\n")
    _git(repository, "config", "filter.audit.clean", str(script))
    (repository / "README.md").write_text("second version\n")

    with pytest.raises(ValueError, match="repository content filters are not allowed"):
        await _tools()[tool_name].handler({}, _context(repository))

    assert not marker.exists(), f"{tool_name} ran a clean filter"


async def test_process_filter_is_refused_before_git_diff(repository: Path) -> None:
    script, marker = _helper(repository, "process-filter")
    (repository / ".gitattributes").write_text("README.md filter=audit\n")
    _git(repository, "config", "filter.audit.process", str(script))
    (repository / "README.md").write_text("changed\n")

    with pytest.raises(ValueError, match="repository content filters are not allowed"):
        await _tools()["git_diff"].handler({}, _context(repository))

    assert not marker.exists()


async def test_filter_refusal_is_a_structured_tool_error(repository: Path) -> None:
    script, marker = _helper(repository, "refused-filter", "cat")
    _git(repository, "config", "filter.audit.clean", str(script))
    registry = ToolRegistry()
    for tool in _tools().values():
        registry.register(tool)

    result = await ToolExecutor(registry).execute(
        ToolCall("refused", "git_diff", {}), _context(repository)
    )

    assert result.is_error
    assert "content filters are not allowed" in result.output
    assert not marker.exists()


@pytest.mark.parametrize("tool_name", ["git_log", "git_show"])
@pytest.mark.parametrize("custom_pretty", [False, True])
async def test_history_never_starts_signature_verifier(
    repository: Path, tool_name: str, custom_pretty: bool
) -> None:
    tree = (await asyncio.to_thread(
        subprocess.check_output, ["git", "write-tree"], cwd=repository, text=True,
    )).strip()
    commit = (
        f"tree {tree}\n"
        "author Audit <audit@example.invalid> 1 +0000\n"
        "committer Audit <audit@example.invalid> 1 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n"
        " audit-only-invalid-signature\n"
        " -----END PGP SIGNATURE-----\n\n"
        "audit signed commit\n"
    )
    oid = (await asyncio.to_thread(
        subprocess.check_output,
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repository, input=commit, text=True,
    )).strip()
    _git(repository, "update-ref", "refs/heads/main", oid)
    script, marker = _helper(repository, "signature", "cat >/dev/null; exit 1")
    _git(repository, "config", "gpg.program", str(script))
    _git(repository, "config", "log.showSignature", "true")
    if custom_pretty:
        # Signature placeholders trigger verification independently of
        # --show-signature; built-in-looking pretty aliases can be replaced.
        for key in ("format.pretty", "pretty.oneline", "pretty.medium"):
            _git(repository, "config", key, "format:%G? %s")
    arguments = {"revision": "HEAD"} if tool_name == "git_show" else {}

    output = await _tools()[tool_name].handler(arguments, _context(repository))

    assert not marker.exists(), f"{tool_name} ran the configured signature verifier"
    assert "audit signed commit" in output

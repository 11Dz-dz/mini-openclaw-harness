import re

from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResourceExceeded,
    SandboxTimeout,
)
from miniclaw.tools.types import (
    RegisteredTool,
    RiskLevel,
    ToolCapabilities,
    ToolContext,
    ToolSpec,
)
from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path

# A revision may reach git's argv, so it must never look like an option and
# must stay inside the characters git revisions actually use.
REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/^~@{}-]{0,127}")
MAX_LOG_LIMIT = 200

# Repository configuration and attributes may select executable helpers.
# argv guards override local/includes/worktree config; environment guards also
# prevent implicit fetches from starting a transport or remote helper.
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_OPTIONAL_LOCKS": "0",
}
GIT_ALLOWED_ENVIRONMENT = ("PATH", *GIT_ENVIRONMENT)
GIT_GLOBAL_OPTIONS = (
    "--no-pager",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.attributesFile=/dev/null",
    "-c", "diff.external=",
    "-c", "log.showSignature=false",
    "-c", "submodule.recurse=false",
)
RAW_DIFF_OPTIONS = ("--no-ext-diff", "--no-textconv", "--ignore-submodules=all")
CONTENT_FILTER_KEYS = r"^filter\..*\.(clean|smudge|process)$"
# A repository's format.pretty can contain %G? and execute a verifier even
# with --no-show-signature. Supply literal non-verifying placeholders.
SHOW_FORMAT = "--format=tformat:commit %H%nAuthor: %an <%ae>%nDate:   %aI%n%n    %s%n%n%b"

READ_ONLY_INSPECTION = ToolCapabilities(
    filesystem="read",
    network="deny",
    subprocess=True,
    risk_level=RiskLevel.LOW,
    side_effects=False,
    idempotent=True,
)


def _relative_path(context: ToolContext, raw: object, mounts: tuple[str, ...]) -> str:
    resolved = resolve_workspace_path(context.workspace, str(raw), mounts=mounts)
    return resolved.relative_to(context.workspace.resolve()).as_posix()


def _revision(raw: object) -> str:
    value = str(raw)
    if not REVISION.fullmatch(value):
        raise ValueError("invalid git revision")
    return value


def make_git_tools(
    sandbox,
    *,
    timeout_seconds: float = 15.0,
    max_output_bytes: int = 64 * 1024,
    trusted: bool = False,
    mounts: tuple[str, ...] = (),
) -> tuple[RegisteredTool, ...]:
    async def run_git(argv: tuple[str, ...], context: ToolContext) -> str:
        async def execute(arguments: tuple[str, ...]):
            return await sandbox.execute(
                Command(
                    ("git", *GIT_GLOBAL_OPTIONS, *arguments),
                    context.workspace,
                    dict(GIT_ENVIRONMENT),
                ),
                SandboxPolicy(
                    context.workspace,
                    "host" if trusted else "read",
                    "unrestricted" if trusted else "deny",
                    GIT_ALLOWED_ENVIRONMENT,
                    timeout_seconds,
                    max_output_bytes,
                ),
            )

        try:
            if argv[0] in {"status", "diff"}:
                # --no-textconv does not disable clean/process filters applied
                # when Git hashes worktree files. There is no generic command
                # option to turn off every named content filter, so refuse
                # these repositories instead of executing their helper code.
                filters = await execute(
                    ("config", "--name-only", "--get-regexp", CONTENT_FILTER_KEYS)
                )
                if filters.exit_code == 0:
                    raise ValueError("repository content filters are not allowed for read-only inspection")
                if filters.exit_code != 1:
                    return f"git {argv[0]} failed: repository configuration could not be checked"
            result = await execute(argv)
        except FileNotFoundError:
            return "git is not installed"
        except SandboxTimeout:
            return "git timed out"
        except SandboxResourceExceeded:
            return "git output exceeded the size limit"
        except SandboxCapabilityError as error:
            return f"git could not run: {error}"
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            reason = detail[0] if detail else f"exit code {result.exit_code}"
            return f"git {argv[0]} failed: {reason}"
        return result.stdout

    async def git_status(arguments, context: ToolContext) -> str:
        return await run_git(
            ("status", "--porcelain=v1", "--branch", "--ignore-submodules=all"),
            context,
        )

    async def git_log(arguments, context: ToolContext) -> str:
        limit = int(arguments.get("limit", 20))
        if not 1 <= limit <= MAX_LOG_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LOG_LIMIT}")
        argv = ["log", "--oneline", "--no-decorate", "--no-show-signature", *RAW_DIFF_OPTIONS, "-n", str(limit)]
        if arguments.get("path") is not None:
            # "--" keeps a path from ever being parsed as an option.
            argv += ["--", _relative_path(context, arguments["path"], mounts)]
        return await run_git(tuple(argv), context)

    async def git_diff(arguments, context: ToolContext) -> str:
        argv = ["diff", *RAW_DIFF_OPTIONS]
        if bool(arguments.get("staged", False)):
            argv.append("--staged")
        if arguments.get("path") is not None:
            argv += ["--", _relative_path(context, arguments["path"], mounts)]
        return await run_git(tuple(argv), context)

    async def git_show(arguments, context: ToolContext) -> str:
        return await run_git(
            ("show", SHOW_FORMAT, "--no-show-signature", *RAW_DIFF_OPTIONS, "--stat", "--patch", _revision(arguments["revision"])),
            context,
        )

    optional_path = {"type": "string"}
    definitions = (
        (
            "git_status",
            "Show the working tree status of the workspace repository",
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            git_status,
        ),
        (
            "git_log",
            "List recent commits, optionally for one path",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "path": optional_path,
                },
                "required": [],
                "additionalProperties": False,
            },
            git_log,
        ),
        (
            "git_diff",
            "Show unstaged or staged changes, optionally for one path",
            {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "path": optional_path,
                },
                "required": [],
                "additionalProperties": False,
            },
            git_diff,
        ),
        (
            "git_show",
            "Show one commit with its stat summary and patch",
            {
                "type": "object",
                "properties": {"revision": {"type": "string"}},
                "required": ["revision"],
                "additionalProperties": False,
            },
            git_show,
        ),
    )
    return tuple(
        RegisteredTool(
            ToolSpec(name, description + (" (trusted host execution; approval required)" if trusted else ""), schema,
                     ToolCapabilities(filesystem="read", network="unrestricted", subprocess=True,
                                      risk_level=RiskLevel.HIGH, side_effects=True, idempotent=False)
                     if trusted else READ_ONLY_INSPECTION),
            handler,
        )
        for name, description, schema, handler in definitions
    )


__all__ = ["make_git_tools", "REVISION", "READ_ONLY_INSPECTION", "WorkspaceEscape"]

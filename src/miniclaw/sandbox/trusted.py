"""Explicit host execution for trusted projects, without sandbox guarantees."""

import asyncio
import contextlib
import os
import signal
import stat
import tempfile
from pathlib import Path

from miniclaw.context.types import ContextSource
from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.output import communicate_bounded
from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResult,
    SandboxTimeout,
)
from miniclaw.workspace.diff import changed_paths, snapshot_files
from miniclaw.workspace.session import WorkspaceSession

TRUSTED_WARNING = (
    "TRUSTED HOST EXECUTION: no filesystem or network sandbox. Commands have "
    "the execution user's host permissions; cwd is a working copy, not a boundary. "
    "Changes outside the copy do not go through /apply."
)


class TrustedExecutionContext:
    async def build(self, prompt: str) -> tuple[ContextSource, ...]:
        return (ContextSource("execution:trusted", TRUSTED_WARNING, 100, required=True),)


class TrustedProcessExecutor:
    def __init__(self, user: str = "miniclaw-runner") -> None:
        self.user = user
        self.uid: int | None = None
        self.gid: int | None = None
        self.workspace: Path | None = None
        self.home: Path | None = None

    async def capabilities(self) -> SandboxCapabilities:
        reason = TRUSTED_WARNING
        available = False
        if os.name != "posix":
            reason = "trusted host execution currently requires POSIX"
        else:
            import pwd

            try:
                identity = (pwd.getpwnam(self.user) if os.geteuid() == 0
                            else pwd.getpwuid(os.geteuid()))
                if identity.pw_uid == 0 or identity.pw_gid == 0:
                    reason = "trusted execution requires a non-root user and group"
                elif os.geteuid() == 0 and not stat.S_IMODE(Path("/").stat().st_mode) & 0o001:
                    reason = "root directory is not traversable by the execution user; ask the administrator to configure host permissions"
                else:
                    self.uid, self.gid = identity.pw_uid, identity.pw_gid
                    available = True
                    reason += f" user={identity.pw_name} uid={self.uid} gid={self.gid}"
            except KeyError:
                reason = f"trusted execution user does not exist: {self.user}"
        return SandboxCapabilities(
            available, SandboxStrength.TRUSTED, False, False, False, reason,
        )

    def create_session(self, root: Path, source: Path) -> WorkspaceSession:
        if self.uid is None or self.gid is None:
            raise SandboxCapabilityError("trusted execution identity is unavailable")
        if os.geteuid() == 0:
            # /root is normally 0700. Do not widen it or grant the runner access
            # to the real project, checkpoints, API keys, or approval records.
            root = Path(tempfile.mkdtemp(prefix="miniclaw-trusted-", dir="/var/tmp"))
            root.chmod(0o711)
        session = WorkspaceSession.create(root, source)
        session.root.chmod(0o711)
        session.artifacts.chmod(0o700)
        self.workspace = session.workspace.resolve()
        self.home = session.root / "command-home"
        self.home.mkdir(mode=0o700)
        (self.home / "tmp").mkdir(mode=0o700)
        if os.geteuid() == 0:
            # Initial copy only: never recursively chown a tree after running
            # user commands, which could have inserted links or moved paths.
            for current, directories, files in os.walk(self.workspace, followlinks=False):
                for name in [*directories, *files]:
                    os.chown(Path(current) / name, self.uid, self.gid,
                             follow_symlinks=False)
            for path in (self.workspace, self.home, self.home / "tmp"):
                os.chown(path, self.uid, self.gid)
        self.workspace.chmod(0o700)
        return session

    async def execute(self, command: Command, policy: SandboxPolicy) -> SandboxResult:
        report = await self.capabilities()
        if not report.available:
            raise SandboxCapabilityError(report.reason)
        if policy.filesystem != "host" or policy.network != "unrestricted":
            raise SandboxCapabilityError("trusted executor requires explicit host access policy")
        workspace = policy.workspace.resolve()
        if self.workspace != workspace or self.home is None:
            raise SandboxCapabilityError("trusted executor is not bound to this workspace copy")
        cwd = command.cwd.resolve()
        if not cwd.is_relative_to(workspace):
            raise SandboxCapabilityError("command cwd is outside the workspace copy")
        if not command.argv or any(not isinstance(arg, str) or not arg for arg in command.argv):
            raise ValueError("argv must contain non-empty strings")
        if policy.timeout_seconds <= 0 or policy.max_output_bytes <= 0:
            raise ValueError("command limits must be positive")
        allowed = set(policy.allowed_environment)
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update((key, value) for key, value in command.environment.items() if key in allowed)
        environment.update(HOME=str(self.home), TMPDIR=str(self.home / "tmp"))
        kwargs = {}
        if os.geteuid() == 0:
            kwargs.update(user=self.uid, group=self.gid, extra_groups=[])
        before = snapshot_files(workspace)
        process = await asyncio.create_subprocess_exec(
            *command.argv, cwd=cwd, env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, umask=0o077, **kwargs,
        )

        def stop_group():
            # Also clean ordinary background children after a successful exit.
            # A process that deliberately changes session can escape this group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

        try:
            stdout, stderr = await communicate_bounded(
                process, policy.max_output_bytes, policy.timeout_seconds,
                terminate=stop_group,
            )
        except TimeoutError as error:
            raise SandboxTimeout("trusted command exceeded time limit") from error
        finally:
            stop_group()
        return SandboxResult(
            process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"),
            changed_paths(before, snapshot_files(workspace)), (report.reason,),
        )

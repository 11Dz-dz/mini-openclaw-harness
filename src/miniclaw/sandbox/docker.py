import asyncio
import os
import re
from pathlib import PurePosixPath
from uuid import uuid4

from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.output import communicate_bounded
from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResourceExceeded,
    SandboxResult,
    SandboxTimeout,
)
from miniclaw.workspace.diff import changed_paths, snapshot_files


class DockerSandbox:
    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        allowed_images: tuple[str, ...] = ("python:3.12-slim",),
        workspace_aliases: tuple[str, ...] = (),
    ) -> None:
        if image not in allowed_images:
            raise ValueError("docker image is not allowlisted")
        if any(re.fullmatch(r"/[a-zA-Z0-9_-]+", alias) is None for alias in workspace_aliases):
            raise ValueError("Docker workspace aliases must be top-level container paths")
        self.image = image
        self.workspace_mounts = tuple(dict.fromkeys(("/workspace", *workspace_aliases)))

    async def capabilities(self) -> SandboxCapabilities:
        for argv, failure_reason in (
            (
                ("docker", "version", "--format", "{{.Server.Version}}"),
                "docker daemon is unavailable",
            ),
            (
                ("docker", "image", "inspect", self.image),
                "configured docker image is unavailable",
            ),
        ):
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await communicate_bounded(process, 65536, 3.0)
            except FileNotFoundError:
                return self._unavailable("docker CLI is unavailable")
            except TimeoutError:
                return self._unavailable("docker capability check timed out")
            except SandboxResourceExceeded:
                return self._unavailable("docker capability output exceeded limit")
            if process.returncode != 0:
                return self._unavailable(failure_reason)
        return SandboxCapabilities(
            True,
            SandboxStrength.STRONG,
            True,
            True,
            True,
            "docker strong sandbox is available",
        )

    async def execute(
        self,
        command: Command,
        policy: SandboxPolicy,
    ) -> SandboxResult:
        report = await self.capabilities()
        if not report.available:
            raise SandboxCapabilityError(report.reason)
        if policy.network != "deny":
            raise SandboxCapabilityError(
                "docker backend only accepts deny network policy"
            )
        workspace = policy.workspace.resolve()
        cwd = command.cwd.resolve()
        if not cwd.is_relative_to(workspace):
            raise SandboxCapabilityError(
                "command cwd is outside the workspace"
            )
        relative = cwd.relative_to(workspace)
        container_cwd = str(PurePosixPath(self.workspace_mounts[0]) / PurePosixPath(relative))
        mount_mode = ",readonly" if policy.filesystem == "read" else ""
        mounts = tuple(
            item
            for path in self.workspace_mounts
            for item in ("--mount", f"type=bind,src={workspace},dst={path}{mount_mode}")
        )
        allowed = set(policy.allowed_environment)
        environment = {
            name: value for name, value in os.environ.items() if name in allowed
        }
        environment.update(
            (name, value)
            for name, value in command.environment.items()
            if name in allowed
        )
        # Preserve explicit empty values (e.g. GIT_ALLOW_PROTOCOL=) and pass
        # only policy-allowlisted entries into the container. Otherwise Git's
        # safety environment would silently disappear at the Docker boundary.
        environment_args = tuple(
            item
            for name, value in sorted(environment.items())
            for item in ("--env", f"{name}={value}")
        )
        container_name = f"miniclaw-command-{uuid4().hex}"
        argv = (
            "docker",
            "run",
            "--rm",
            "--name", container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--pids-limit",
            "64",
            "--memory",
            "512m",
            "--cpus",
            "1",
            *mounts,
            "--workdir",
            container_cwd,
            *environment_args,
            self.image,
            *command.argv,
        )
        before = snapshot_files(workspace)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await communicate_bounded(
                process, policy.max_output_bytes, policy.timeout_seconds)
        except TimeoutError as exc:
            raise SandboxTimeout("docker process exceeded time limit") from exc
        finally:
            # A killed Docker CLI may leave its container running.
            cleanup = await asyncio.create_subprocess_exec(
                'docker', 'rm', '-f', container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(cleanup.wait(), 3)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
        after = snapshot_files(workspace)
        return SandboxResult(
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            changed_paths(before, after),
        )

    @staticmethod
    def _unavailable(reason: str) -> SandboxCapabilities:
        return SandboxCapabilities(
            False,
            SandboxStrength.STRONG,
            False,
            False,
            False,
            reason,
        )

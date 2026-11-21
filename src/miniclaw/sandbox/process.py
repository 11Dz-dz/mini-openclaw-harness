from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.types import (
    Command,
    SandboxCapabilityError,
    SandboxPolicy,
    SandboxResult,
)

PROCESS_UNAVAILABLE_REASON = (
    "process execution is unavailable: filesystem and network isolation "
    "cannot be enforced; configure an isolated execution backend"
)


class ProcessSandbox:
    """Refuse execution until an OS-enforced process sandbox is implemented.

    A cwd, filtered environment and process-group cleanup do not constrain
    filesystem access or network connections. A root process inside a
    container can still overwrite that container's real project.
    Approval cannot supply the missing isolation guarantees.
    """

    async def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            False,
            SandboxStrength.PROCESS,
            False,
            False,
            False,
            PROCESS_UNAVAILABLE_REASON,
        )

    async def execute(
        self,
        command: Command,
        policy: SandboxPolicy,
    ) -> SandboxResult:
        raise SandboxCapabilityError(PROCESS_UNAVAILABLE_REASON)

from miniclaw.sandbox.base import SandboxExecutor
from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.tools.types import ToolCapabilities


class SandboxCapabilityUnavailable(RuntimeError):
    pass


class SandboxRouter:
    def __init__(
        self,
        *,
        process: SandboxExecutor | None = None,
        strong=None,
        trusted=None,
    ) -> None:
        self.process = process or ProcessSandbox()
        self.strong = strong
        self.trusted = trusted
        self.selected = "restricted"
        self.unavailable_reason: str | None = None

    async def select(
        self,
        capabilities: ToolCapabilities,
        require_strong: bool,
        mode: str = "auto",
    ) -> SandboxExecutor:
        self.selected = "restricted"
        self.unavailable_reason = None
        if mode not in {"auto", "restricted", "trusted"}:
            raise ValueError("unknown execution mode")
        if require_strong and mode != "auto":
            raise ValueError("require_strong_sandbox requires execution_mode=auto")
        if mode == "restricted":
            self.unavailable_reason = "local command execution explicitly disabled"
            raise SandboxCapabilityUnavailable(self.unavailable_reason)
        if mode == "trusted":
            report = await self.trusted.capabilities() if self.trusted else None
            if report is None or not report.available:
                self.unavailable_reason = report.reason if report else "trusted executor is not configured"
                raise SandboxCapabilityUnavailable(self.unavailable_reason)
            self.selected = "trusted"
            return self.trusted
        if not require_strong:
            report = await self.process.capabilities()
            if report.available and report.network_isolation and report.read_only_root:
                self.selected = "process"
                return self.process
        if self.strong is None:
            self.unavailable_reason = "isolated execution backend is not configured"
            raise SandboxCapabilityUnavailable(self.unavailable_reason)
        report = await self.strong.capabilities()
        if not (report.available and report.network_isolation and report.read_only_root):
            self.unavailable_reason = report.reason
            raise SandboxCapabilityUnavailable(self.unavailable_reason)
        self.selected = "strong"
        return self.strong

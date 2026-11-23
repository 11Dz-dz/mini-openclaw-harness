from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunStarted:
    prompt: str


@dataclass(frozen=True, slots=True)
class ContextBuilt:
    message_count: int
    estimated_tokens: int | None = None
    dropped_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelStreamStarted:
    turn: int


@dataclass(frozen=True, slots=True)
class ModelResponseFinished:
    turn: int
    reason: str
    reasoning_tokens: int | None
    reasoning_chars: int
    visible_chars: int
    tool_fragments: int


@dataclass(frozen=True, slots=True)
class ArtifactsReady:
    turn: int


@dataclass(frozen=True, slots=True)
class ReasoningRecovery:
    turn: int
    recovered: bool


@dataclass(frozen=True, slots=True)
class RunCompleted:
    output: str


@dataclass(frozen=True, slots=True)
class CompletionChecked:
    passed: bool


@dataclass(frozen=True, slots=True)
class WorkspaceVerified:
    passed: bool


@dataclass(frozen=True, slots=True)
class VerificationStalled:
    consecutive_checks: int


@dataclass(frozen=True, slots=True)
class RunCancelled:
    reason: str = "cancelled"


@dataclass(frozen=True, slots=True)
class RunFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class RunEnded:
    status: str


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    request_id: str
    tool_call_id: str
    tool_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    request_id: str
    approved: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ContextInjected:
    source_ids: tuple[str, ...]

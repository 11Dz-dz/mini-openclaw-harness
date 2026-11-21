from dataclasses import dataclass, field

from miniclaw.observability.types import RunMetrics


@dataclass(frozen=True, slots=True)
class EvalAssertion:
    kind: str
    expected: object


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    prompt: str
    script: tuple[dict[str, object], ...]
    assertions: tuple[EvalAssertion, ...]
    fault_plan: tuple[dict[str, object], ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    approved_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvalResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    metrics: RunMetrics
    run_id: str

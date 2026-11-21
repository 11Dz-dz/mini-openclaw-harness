import json
from pathlib import Path

from miniclaw.evals.types import EvalAssertion, EvalCase


class EvalLoadError(ValueError):
    pass


_ASSERTIONS = {
    "output_equals",
    "status_equals",
    "tool_called",
    "tool_not_called",
    "approval_count",
    "sandbox_violation_count",
    "checkpoint_count",
    "event_sequence_contains",
    "no_secret_in_trace",
}
_FAULTS = {
    "model_disconnect_after_event",
    "tool_raise",
    "tool_timeout",
    "sandbox_violation",
}


def load_eval_cases(path: Path) -> tuple[EvalCase, ...]:
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise EvalLoadError("invalid eval JSON") from exc
    if not isinstance(payload, list):
        raise EvalLoadError("eval file must contain a list")
    cases = []
    identifiers: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict) or not set(raw).issubset(
            {"id", "prompt", "script", "assertions", "fault_plan", "files", "approved_tools"}
        ):
            raise EvalLoadError("unknown eval case fields")
        if not {"id", "prompt", "script", "assertions"}.issubset(raw):
            raise EvalLoadError("missing eval case fields")
        case_id = raw["id"]
        prompt = raw["prompt"]
        script = raw["script"]
        assertions = raw["assertions"]
        faults = raw.get("fault_plan", [])
        if not isinstance(case_id, str) or not isinstance(prompt, str):
            raise EvalLoadError("invalid eval strings")
        if len(case_id) > 64 * 1024 or len(prompt) > 64 * 1024:
            raise EvalLoadError("eval string exceeds limit")
        if case_id in identifiers:
            raise EvalLoadError(f"duplicate eval id: {case_id}")
        identifiers.add(case_id)
        if not isinstance(script, list) or len(script) > 128:
            raise EvalLoadError("invalid eval script")
        if not isinstance(assertions, list) or not assertions:
            raise EvalLoadError("eval assertions are required")
        if not isinstance(faults, list):
            raise EvalLoadError("invalid fault plan")
        decoded_assertions = []
        for assertion in assertions:
            if (
                not isinstance(assertion, dict)
                or set(assertion) != {"type", "value"}
                or assertion["type"] not in _ASSERTIONS
            ):
                raise EvalLoadError("unknown eval assertion")
            decoded_assertions.append(
                EvalAssertion(str(assertion["type"]), assertion["value"])
            )
        for fault in faults:
            if (
                not isinstance(fault, dict)
                or fault.get("type") not in _FAULTS
            ):
                raise EvalLoadError("unsupported fault kind")
        files = raw.get('files', {})
        approved = raw.get('approved_tools', [])
        if (not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str)
                for k, v in files.items()) or not isinstance(approved, list)
                or not all(isinstance(name, str) for name in approved)):
            raise EvalLoadError('invalid eval fixtures or approved tools')
        cases.append(
            EvalCase(
                case_id,
                prompt,
                tuple(script),
                tuple(decoded_assertions),
                tuple(faults),
                files,
                tuple(approved),
            )
        )
    return tuple(cases)

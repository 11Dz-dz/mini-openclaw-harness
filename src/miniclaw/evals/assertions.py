from miniclaw.agent.loop import RunResult
from miniclaw.evals.types import EvalAssertion


def evaluate_assertion(
    assertion: EvalAssertion,
    result: RunResult,
    observations: dict | None = None,
) -> str | None:
    observations = observations or {}
    if assertion.kind == "output_equals":
        actual = result.output
    elif assertion.kind == "status_equals":
        actual = result.status.value
    elif assertion.kind == "event_sequence_contains":
        actual = [event.payload.__class__.__name__ for event in result.events]
        expected = assertion.expected
        remaining = iter(actual)
        if not isinstance(expected, list) or not all(
            any(candidate == item for candidate in remaining) for item in expected):
            return "event sequence does not contain expected event types"
        return None
    elif assertion.kind in {'tool_called', 'tool_not_called'}:
        called = assertion.expected in observations.get('called_tools', ())
        actual = called if assertion.kind == 'tool_called' else not called
        if not actual:
            return f'{assertion.kind} failed for {assertion.expected!r}'
        return None
    elif assertion.kind in {'approval_count', 'sandbox_violation_count', 'checkpoint_count'}:
        if assertion.kind not in observations:
            return f'missing observation: {assertion.kind}'
        actual = observations[assertion.kind]
    elif assertion.kind == 'no_secret_in_trace':
        secrets = assertion.expected if isinstance(assertion.expected, list) else [assertion.expected]
        if any(not isinstance(secret, str) or not secret for secret in secrets):
            return 'no_secret_in_trace requires nonempty synthetic secrets'
        if any(secret in observations.get('trace_text', '') for secret in secrets):
            return 'secret found in persisted trace'
        return None
    else:
        return f"assertion kind is not supported by this runner: {assertion.kind}"
    if actual != assertion.expected:
        return f"{assertion.kind} expected {assertion.expected!r}, got {actual!r}"
    return None

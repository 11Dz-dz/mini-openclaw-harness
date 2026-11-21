import pytest

from miniclaw.evals.loader import EvalLoadError
from miniclaw.evals.runner import EvalRunner
from miniclaw.evals.types import EvalAssertion, EvalCase


def read_script():
    return ({'type': 'tool_delta', 'id': 'one', 'name': 'read_file',
             'arguments': '{"path":"fixture.txt"}'},
            {'type': 'complete', 'reason': 'tool_calls'},
            {'type': 'text', 'text': 'done'}, {'type': 'complete', 'reason': 'stop'})


async def test_all_assertion_kinds_use_real_offline_observations():
    case = EvalCase('observed', 'read', read_script(), (
        EvalAssertion('output_equals', 'done'), EvalAssertion('status_equals', 'completed'),
        EvalAssertion('tool_called', 'read_file'), EvalAssertion('tool_not_called', 'write_file'),
        EvalAssertion('approval_count', 0), EvalAssertion('sandbox_violation_count', 0),
        EvalAssertion('checkpoint_count', 5),
        EvalAssertion('event_sequence_contains', ['RunStarted', 'ToolCallDelta', 'RunCompleted']),
        EvalAssertion('no_secret_in_trace', 'synthetic-secret'),
    ), files={'fixture.txt': 'synthetic-secret'})
    result = await EvalRunner().run(case)
    assert result.passed, result.failures


async def test_wrong_event_order_fails_even_when_all_types_exist():
    result = await EvalRunner().run(EvalCase('order', 'go', read_script(), (
        EvalAssertion('event_sequence_contains', ['RunCompleted', 'RunStarted']),)))
    assert not result.passed


@pytest.mark.parametrize('kind', ['tool_raise', 'tool_timeout', 'sandbox_violation'])
async def test_tool_faults_actually_trigger(kind):
    assertions = [EvalAssertion('tool_called', 'read_file')]
    if kind == 'sandbox_violation':
        assertions.append(EvalAssertion('sandbox_violation_count', 1))
    result = await EvalRunner().run(EvalCase('fault', 'read', read_script(),
        tuple(assertions), ({'type': kind, 'tool': 'read_file'},),
        files={'fixture.txt': 'ok'}))
    assert result.passed, result.failures


async def test_unused_fault_cannot_silently_pass():
    result = await EvalRunner().run(EvalCase('unused', 'go',
        ({'type': 'complete', 'reason': 'stop'},),
        (EvalAssertion('status_equals', 'completed'),), ({'type': 'tool_raise'},)))
    assert not result.passed
    assert 'not triggered' in result.failures[0]


async def test_disconnect_is_wired_into_the_runner():
    result = await EvalRunner().run(EvalCase('disconnect', 'go', read_script(),
        (EvalAssertion('status_equals', 'failed'),),
        ({'type': 'model_disconnect_after_event', 'after_event': 1},)))
    assert result.passed, result.failures


async def test_unimplemented_fault_is_rejected_before_execution():
    with pytest.raises(EvalLoadError, match='unsupported fault'):
        await EvalRunner().run(EvalCase('unsupported', 'go', (),
            (EvalAssertion('status_equals', 'completed'),),
            ({'type': 'process_interrupt_after_checkpoint'},)))

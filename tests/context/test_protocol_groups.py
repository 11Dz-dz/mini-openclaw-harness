from miniclaw.context.builder import ContextBuilder, sources_from_messages
from miniclaw.context.history import select_request_history
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget
from miniclaw.core.messages import Message, TextContent, ToolCall, ToolResultContent
from miniclaw.sessions.recovery import close_interrupted_tool_calls


def test_current_task_compaction_drops_old_calls_and_results_together():
    messages = (
        Message('user', (TextContent('fix this'),)),
        Message('assistant', (ToolCall('old', 'read_file', {}),)),
        Message('tool', (ToolResultContent('old', 'x' * 4000),)),
        Message('assistant', (ToolCall('new', 'read_file', {}),)),
        Message('tool', (ToolResultContent('new', 'recent'),)),
    )
    snapshot, request = select_request_history(
        ContextBuilder(CharacterTokenEstimator()), messages, (), ContextBudget(700, 100),
    )
    assert request == messages
    assert [source.source_id for source in snapshot.items] == ['message:0', 'message:3', 'message:4']


def test_oversized_latest_batch_shortens_only_request_results():
    original = ToolResultContent('one', '{"exit_code":1,"stderr":"' + 'x' * 4000 + ' tail"}')
    messages = (
        Message('user', (TextContent('fix this'),)),
        Message('assistant', (ToolCall('one', 'run_command', {}), ToolCall('two', 'read_file', {}))),
        Message('tool', (original,)),
        Message('tool', (ToolResultContent('two', 'second'),)),
    )
    snapshot, request = select_request_history(
        ContextBuilder(CharacterTokenEstimator()), messages, (), ContextBudget(850, 100),
    )
    assert snapshot.dropped_source_ids == ()
    assert request[2].content[0].tool_call_id == 'one'
    assert 'tail' in request[2].content[0].output and '"exit_code": 1' in request[2].content[0].output
    assert len(request[2].content[0].output) < 800
    assert messages[2].content[0] == original


def test_budget_never_keeps_a_small_call_without_its_large_result():
    messages = (
        Message('user', (TextContent('old request'),)),
        Message('assistant', (ToolCall('old', 'read_file', {}),)),
        Message('tool', (ToolResultContent('old', 'x' * 8000),)),
        Message('user', (TextContent('current request'),)),
    )
    context = ContextBuilder(CharacterTokenEstimator()).build(
        sources_from_messages(messages), ContextBudget(100, 10))
    assert [s.source_id for s in context.items] == ['message:3']


def test_resume_closes_pending_calls_in_place_without_replaying():
    messages = (Message('assistant', (ToolCall('one', 'write', {}),
                                      ToolCall('two', 'write', {}))),
                Message('tool', (ToolResultContent('one', 'done'),)),
                Message('user', (TextContent('continue'),)))
    restored = close_interrupted_tool_calls(messages)
    assert restored[2].content[0].tool_call_id == 'two'
    assert restored[2].content[0].is_error
    assert 'not replayed' in restored[2].content[0].output
    assert restored[-1] == messages[-1]

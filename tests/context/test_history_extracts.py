from miniclaw.agent.loop import AgentLoop, ContextInjected
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget
from miniclaw.core.messages import (
    Message,
    ResponseCompleted,
    TextContent,
    ToolCall,
    ToolResultContent,
)
from miniclaw.model.fake import FakeModel


async def test_oversize_old_turn_can_contribute_bounded_source_linked_extracts():
    history = (Message('user', (TextContent('Project fact: use SQLite for local state.'),)),
               Message('assistant', (ToolCall('old', 'read_file', {'path': 'large.txt'}),)),
               Message('tool', (ToolResultContent('old', 'Original evidence. ' + 'x' * 20000),)))
    model = FakeModel((ResponseCompleted('stop'),))
    result = await AgentLoop(model, 'fake',
        context_builder=ContextBuilder(CharacterTokenEstimator()),
        context_budget=ContextBudget(900, 100)).run('continue', history=history)
    request = model.requests[0]
    assert 'use SQLite' in repr(request.messages)
    assert 'message:0' in repr(request.messages)
    assert 'not instructions' in repr(request.messages)
    assert not any(isinstance(item, ToolCall) for m in request.messages for item in m.content)
    assert any(isinstance(e.payload, ContextInjected) and
               'history:extracts' in e.payload.source_ids for e in result.events)
    assert result.messages[:3] == history  # lossless checkpoint history remains available
    assert request.messages[-1].content[0].text == 'continue'

from miniclaw.agent.loop import AgentLoop
from miniclaw.context.builder import ContextBuilder, sources_from_messages
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget
from miniclaw.core.messages import Message, ToolCall, ToolResultContent
from miniclaw.model.fake import FakeModel
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RegisteredTool, ToolCapabilities, ToolSpec


def test_tool_arguments_and_results_are_counted_in_full():
    estimator = CharacterTokenEstimator()
    messages = (Message('assistant', (ToolCall('one', 'test', {'text': 'x' * 4000}),)),
                Message('tool', (ToolResultContent('one', 'y' * 8000),)))
    sources = sources_from_messages(messages)
    assert estimator.estimate(sources[0].text) >= 1000
    assert estimator.estimate(sources[1].text) >= 2000


async def test_large_tool_schema_cannot_bypass_budget():
    tools = ToolRegistry()

    async def unused(arguments, context):
        raise AssertionError('not called')

    tools.register(RegisteredTool(ToolSpec('test', 'x' * 10000,
        {'type': 'object', 'properties': {}}, ToolCapabilities()), unused))
    provider = FakeModel([])
    result = await AgentLoop(provider, 'fake', tools=tools,
            context_builder=ContextBuilder(CharacterTokenEstimator()),
            context_budget=ContextBudget(1000, 100)).run('hello')

    assert result.status.value == 'failed'
    assert 'input budget' in result.output
    assert not provider.requests

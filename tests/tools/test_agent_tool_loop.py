from miniclaw.agent.loop import AgentLoop
from miniclaw.agent.state import RunStatus
from miniclaw.core.messages import (
    ResponseCompleted,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolResultContent,
)
from miniclaw.model.scripted import ScriptedModel
from miniclaw.sessions.types import Checkpoint
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RegisteredTool, ToolCapabilities, ToolSpec


async def test_agent_executes_tool_once_and_continues() -> None:
    calls: list[str] = []

    async def echo(arguments, context) -> str:
        calls.append(str(arguments["text"]))
        return str(arguments["text"])

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            ToolSpec(
                "echo",
                "Echo text",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                ToolCapabilities(),
            ),
            echo,
        )
    )
    provider = ScriptedModel(
        (
            (
                ToolCallDelta(0, "call-1", "echo", '{"text":"hi"}'),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("done"), ResponseCompleted("stop")),
        )
    )
    loop = AgentLoop(provider, "fake", tools=registry)

    result = await loop.run("use echo")

    assert result.status is RunStatus.COMPLETED
    assert result.output == "done"
    assert calls == ["hi"]
    second_request = provider.requests[1]
    assert any(
        isinstance(item, ToolCall)
        for message in second_request.messages
        for item in message.content
    )
    assert any(
        isinstance(item, ToolResultContent)
        for message in second_request.messages
        for item in message.content
    )


async def test_each_tool_result_is_checkpointed_before_next_dispatch() -> None:
    saved: list[Checkpoint] = []

    class CheckpointRecorder:
        async def save(self, checkpoint: Checkpoint) -> None:
            saved.append(checkpoint)

    async def first(_arguments, _context) -> str:
        return "first result"

    async def second(_arguments, _context) -> str:
        assert saved[-1].completed_tool_executions == ("call-1",)
        assert saved[-1].messages[-1].content == (ToolResultContent("call-1", "first result"),)
        return "second result"

    registry = ToolRegistry()
    for name, handler in (("first", first), ("second", second)):
        registry.register(RegisteredTool(
            ToolSpec(name, name, {"type": "object"}, ToolCapabilities()), handler,
        ))
    provider = ScriptedModel((
        (ToolCallDelta(0, "call-1", "first", "{}"), ToolCallDelta(1, "call-2", "second", "{}"),
         ResponseCompleted("tool_calls")),
        (TextDelta("done"), ResponseCompleted("stop")),
    ))
    result = await AgentLoop(provider, "fake", tools=registry, checkpoint_store=CheckpointRecorder()).run("run both")

    assert result.status is RunStatus.COMPLETED
    assert saved[-1].completed_tool_executions == ("call-1", "call-2")

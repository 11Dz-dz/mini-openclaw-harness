from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from miniclaw.agent.events import ModelResponseFinished, ModelUsage
from miniclaw.core.errors import ToolArgumentsInvalid
from miniclaw.core.messages import (
    ModelRequest,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ToolCallDelta,
)
from miniclaw.model.base import ModelProvider
from miniclaw.tools.assembler import ToolCallAssembler


@dataclass(frozen=True, slots=True)
class ModelTurn:
    finish_reason: str | None
    answer: tuple[str, ...]
    reasoning: tuple[str, ...]
    reasoning_tokens: int | None
    tool_fragments: int
    pending_tool_names: frozenset[str]
    assembly_error: ToolArgumentsInvalid | None


async def receive_model_turn(
    provider: ModelProvider,
    request: ModelRequest,
    assembler: ToolCallAssembler,
    available_tool_names: set[str],
    turn: int,
    emit: Callable[[object], Awaitable[None]],
) -> ModelTurn:
    answer: list[str] = []
    reasoning: list[str] = []
    assembly_error: ToolArgumentsInvalid | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    tool_fragments = 0
    pending_tool_names: set[str] = set()
    async for event in provider.stream(request):
        if isinstance(event, TextDelta):
            answer.append(event.text)
            await emit(event)
        elif isinstance(event, ReasoningDelta):
            # Keep provider state for continuation without recording it in the audit stream.
            reasoning.append(event.text)
        elif isinstance(event, ToolCallDelta):
            tool_fragments += 1
            if event.name in available_tool_names:
                pending_tool_names.add(event.name)
            if assembly_error is None:
                try:
                    assembler.feed(event)
                except ToolArgumentsInvalid as error:
                    assembly_error = error
            await emit(event)
        elif isinstance(event, ResponseCompleted):
            finish_reason = event.finish_reason
            if event.usage is not None:
                await emit(ModelUsage(event.usage.input_tokens, event.usage.output_tokens))
                reasoning_tokens = event.usage.reasoning_tokens
            reason = finish_reason if finish_reason in {"stop", "length", "tool_calls", "content_filter"} else "other"
            await emit(ModelResponseFinished(
                turn, reason, reasoning_tokens,
                sum(map(len, reasoning)), sum(map(len, answer)), tool_fragments,
            ))
            break
    return ModelTurn(finish_reason, tuple(answer), tuple(reasoning), reasoning_tokens,
                     tool_fragments, frozenset(pending_tool_names), assembly_error)

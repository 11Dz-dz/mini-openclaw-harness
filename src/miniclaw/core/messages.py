from dataclasses import dataclass, field
from typing import Literal, TypeAlias

Role: TypeAlias = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolResultContent:
    tool_call_id: str
    output: str
    is_error: bool = False
    execution_id: str | None = None


Content: TypeAlias = TextContent | ToolCall | ToolResultContent


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: tuple[Content, ...]
    # Provider protocol state, not user-visible answer text or audit output.
    reasoning_content: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, object], ...] = ()
    temperature: float = 0.0
    max_output_tokens: int | None = None
    tool_choice: Literal["required"] | None = None
    enable_thinking: bool | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    call_id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    finish_reason: str
    usage: Usage | None = None


ModelEvent: TypeAlias = TextDelta | ReasoningDelta | ToolCallDelta | ResponseCompleted | Usage

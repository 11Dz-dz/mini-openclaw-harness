from miniclaw.core.errors import ModelProtocolError
from miniclaw.core.messages import (
    ModelEvent,
    ReasoningDelta,
    ResponseCompleted,
    TextDelta,
    ToolCallDelta,
    Usage,
)


def decode_openai_chunk(payload: dict[str, object]) -> tuple[ModelEvent, ...]:
    if payload.get('choices') == [] and payload.get('usage') is not None:
        return (_usage(payload['usage']),)
    try:
        choices = payload["choices"]
        if not isinstance(choices, list):
            raise TypeError
        choice = choices[0]
        if not isinstance(choice, dict):
            raise TypeError
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise TypeError
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelProtocolError("invalid streaming chunk") from exc

    events: list[ModelEvent] = []
    reasoning = delta.get("reasoning_content")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            raise ModelProtocolError("invalid streaming reasoning content")
        events.append(ReasoningDelta(reasoning))
    content = delta.get("content")
    if isinstance(content, str) and content:
        events.append(TextDelta(content))

    raw_tool_calls = delta.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        raise ModelProtocolError("invalid streaming tool calls")
    for raw in raw_tool_calls:
        if not isinstance(raw, dict):
            raise ModelProtocolError("invalid streaming tool call")
        function = raw.get("function") or {}
        if not isinstance(function, dict) or not isinstance(raw.get("index"), int):
            raise ModelProtocolError("invalid streaming tool call")
        # Some compatible providers use empty strings instead of null for
        # identity fields on continuation chunks.
        call_id = raw.get("id")
        name = function.get("name")
        events.append(
            ToolCallDelta(
                index=raw["index"],
                call_id=call_id if isinstance(call_id, str) and call_id else None,
                name=name if isinstance(name, str) and name else None,
                arguments_fragment=(
                    function.get("arguments")
                    if isinstance(function.get("arguments"), str)
                    else ""
                ),
            )
        )

    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason:
        raw_usage = payload.get("usage")
        usage = None
        if raw_usage is not None:
            try:
                usage = _usage(raw_usage)
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelProtocolError("invalid streaming usage") from exc
        events.append(ResponseCompleted(finish_reason, usage))
    return tuple(events)


def _usage(raw):
    if (not isinstance(raw, dict)
            or type(raw.get('prompt_tokens')) is not int
            or type(raw.get('completion_tokens')) is not int
            or min(raw['prompt_tokens'], raw['completion_tokens']) < 0):
        raise ModelProtocolError('invalid streaming usage')
    return Usage(raw['prompt_tokens'], raw['completion_tokens'])

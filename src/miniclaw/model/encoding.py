import json

from miniclaw.core.messages import (
    Message,
    ModelRequest,
    TextContent,
    ToolCall,
    ToolResultContent,
)


def encode_message(message: Message) -> dict[str, object]:
    texts = [item.text for item in message.content if isinstance(item, TextContent)]
    assistant_metadata = {}
    if message.reasoning_content is not None:
        if message.role != "assistant" or not isinstance(message.reasoning_content, str):
            raise ValueError("reasoning content must be assistant protocol state")
        assistant_metadata["reasoning_content"] = message.reasoning_content
    tool_calls = [item for item in message.content if isinstance(item, ToolCall)]
    tool_results = [
        item for item in message.content if isinstance(item, ToolResultContent)
    ]
    if tool_calls:
        return {
            "role": message.role,
            **({"content": "".join(texts)} if texts or assistant_metadata else {}),
            **assistant_metadata,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                }
                for call in tool_calls
            ],
        }
    if tool_results:
        if len(tool_results) != 1:
            raise ValueError("tool messages contain exactly one result")
        result = tool_results[0]
        return {
            "role": "tool",
            "tool_call_id": result.tool_call_id,
            "content": (result.output + f"\n[execution evidence: {result.execution_id}]"
                        if result.execution_id else result.output),
        }
    return {"role": message.role, "content": "".join(texts), **assistant_metadata}


def encode_request(request: ModelRequest, *, stream: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": request.model,
        "messages": [encode_message(message) for message in request.messages],
        "temperature": request.temperature,
        "stream": stream,
    }
    if request.tools:
        payload["tools"] = list(request.tools)
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
    if stream:
        payload['stream_options'] = {'include_usage': True}
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    if request.enable_thinking is not None:
        payload["enable_thinking"] = request.enable_thinking
    return payload

import json
import stat

import httpx
import pytest

from miniclaw.agent.loop import AgentLoop
from miniclaw.cli.repl import Repl
from miniclaw.context.builder import sources_from_messages
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.core.errors import ModelProtocolError
from miniclaw.core.messages import (
    Message,
    ReasoningDelta,
    ResponseCompleted,
    TextContent,
    TextDelta,
    ToolCall,
)
from miniclaw.model.encoding import encode_message
from miniclaw.model.fake import FakeModel
from miniclaw.model.openai_compat import OpenAICompatibleClient
from miniclaw.model.openai_stream import decode_openai_chunk
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.sessions.checkpoints import FileCheckpointStore
from miniclaw.sessions.database import SessionRepository
from miniclaw.sessions.serialization import (
    PersistenceError,
    decode_checkpoint,
    encode_checkpoint,
)
from miniclaw.sessions.types import Checkpoint
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.registry import ToolRegistry


def test_reasoning_delta_is_separate_from_visible_text_and_repr():
    events = decode_openai_chunk({"choices": [{"delta": {
        "reasoning_content": "private protocol text", "content": "visible"}, "finish_reason": None}]})
    assert events == (ReasoningDelta("private protocol text"), TextDelta("visible"))
    assert "private protocol text" not in repr(events[0])


@pytest.mark.parametrize("value", [[], {}, 42, True])
def test_invalid_reasoning_chunks_fail(value):
    with pytest.raises(ModelProtocolError, match="reasoning"):
        decode_openai_chunk({"choices": [{"delta": {"reasoning_content": value}}]})


def test_encoding_preserves_thinking_and_tool_turn_visible_content():
    message = Message("assistant", (TextContent("Checking"), ToolCall("call-1", "read_file", {"path": "a"})),
                      reasoning_content="protocol state")
    wire = encode_message(message)
    assert wire["reasoning_content"] == "protocol state"
    assert wire["content"] == "Checking"
    assert wire["tool_calls"][0]["id"] == "call-1"
    assert "protocol state" not in repr(message)
    assert "reasoning_content" not in encode_message(Message("assistant", (TextContent("old"),)))


async def test_deepseek_style_thinking_tool_loop_and_next_user_turn(tmp_path):
    (tmp_path / "note.txt").write_text("fixture content")
    registry = ToolRegistry()
    registry.register(make_file_tools()[0])
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["tools"][0]["function"]["name"] == "read_file"
        if len(requests) == 1:
            deltas = [
                {"reasoning_content": "first "}, {"reasoning_content": "state"},
                {"content": "Checking the file."},
                {"tool_calls": [{"index": 0, "id": "read-1", "function": {
                    "name": "read_file", "arguments": '{"path":"note.txt"}'}}]},
            ]
            reason = "tool_calls"
        else:
            assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
            assert assistants[0]["reasoning_content"] == "first state"
            assert assistants[0]["content"] == "Checking the file."
            assert any(m.get("content") == "fixture content" for m in payload["messages"] if m["role"] == "tool")
            if len(requests) == 3:
                assert assistants[1]["reasoning_content"] == "second state"
            deltas = [{"reasoning_content": "second state"}, {"content": "File read successfully."}]
            reason = "stop"
        chunks = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in deltas]
        chunks.append({"choices": [{"delta": {}, "finish_reason": reason}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 12}})
        return httpx.Response(200, text=''.join(f'data: {json.dumps(chunk)}\n\n' for chunk in chunks) + 'data: [DONE]\n\n')

    client = OpenAICompatibleClient("https://api.deepseek.com", "fixture-only", transport=httpx.MockTransport(handler))
    repository = SessionRepository(tmp_path / "state.db")
    await repository.initialize()
    store = FileCheckpointStore(tmp_path / "checkpoints", repository)
    loop = AgentLoop(client, "deepseek-flash", tools=registry, workspace=tmp_path,
                     permission_policy=DefaultPermissionPolicy(), session_repository=repository, checkpoint_store=store)
    try:
        result = await loop.run("read the note")
        assert result.status == "completed" and result.output == "File read successfully."
        assert result.messages[1].reasoning_content == "first state"
        assert result.messages[-1].reasoning_content == "second state"
        assert not any(isinstance(event.payload, ReasoningDelta) for event in result.events)
        restored = await store.latest_for_run(result.run_id)
        assert restored.messages == result.messages
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (tmp_path / "checkpoints").glob('*.json'))
        next_result = await loop.run("next question", history=restored.messages, session_id=result.session_id)
        assert next_result.status == "completed" and len(requests) == 3
    finally:
        await client.aclose()


def test_old_checkpoint_and_reasoning_metadata_round_trip():
    old = Checkpoint("cp", "run", "session", 1, "completed", (Message("assistant", (TextContent("done"),)),), ())
    payload = encode_checkpoint(old)
    assert set(payload["messages"][0]) == {"role", "content"}
    assert decode_checkpoint(payload) == old
    payload["messages"][0]["reasoning_content"] = "restored state"
    assert decode_checkpoint(payload).messages[0].reasoning_content == "restored state"


@pytest.mark.parametrize("role,value", [("user", "invalid"), ("assistant", {}), ("tool", "invalid")])
def test_checkpoint_rejects_invalid_reasoning_metadata(role, value):
    checkpoint = Checkpoint("cp", "run", "session", 1, "completed", (Message("assistant", ()),), ())
    payload = encode_checkpoint(checkpoint)
    payload["messages"][0].update(role=role, reasoning_content=value)
    with pytest.raises(PersistenceError):
        decode_checkpoint(payload)


def test_reasoning_is_included_in_context_accounting():
    plain = sources_from_messages((Message("assistant", (TextContent("answer"),)),))
    extended = sources_from_messages((Message("assistant", (TextContent("answer"),), "state " * 1000),))
    estimator = CharacterTokenEstimator()
    assert estimator.estimate(extended[0].token_text or extended[0].text) > estimator.estimate(plain[0].text) + 1000


async def test_simple_repl_keeps_protocol_state_without_displaying_it():
    output = []
    repl = Repl(FakeModel([ReasoningDelta("private state"), TextDelta("answer"), ResponseCompleted("stop")]),
                "fake", write=output.append)
    await repl.run_turn("hello")
    assert ''.join(output) == "answer"
    assert repl.messages[-1].reasoning_content == "private state"

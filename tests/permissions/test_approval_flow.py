from miniclaw.agent.loop import AgentLoop, ApprovalRequested, ApprovalResolved
from miniclaw.core.messages import (
    ResponseCompleted,
    TextDelta,
    ToolCallDelta,
    ToolResultContent,
)
from miniclaw.model.scripted import ScriptedModel
from miniclaw.permissions.approval import InMemoryApprovalProvider
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.types import PermissionRequest
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RegisteredTool, RiskLevel, ToolCapabilities, ToolSpec


async def test_approval_is_bound_to_one_request() -> None:
    provider = InMemoryApprovalProvider({"approval-1": True})
    request = PermissionRequest(
        "approval-1",
        "run-1",
        "call-1",
        "run_command",
        '{"argv":["python","-V"]}',
        ToolCapabilities(
            filesystem="workspace-write",
            subprocess=True,
            risk_level=RiskLevel.HIGH,
            side_effects=True,
            idempotent=False,
        ),
        "subprocess with side effects",
    )

    first = await provider.resolve(request)
    second = await provider.resolve(request)

    assert first.approved is True
    assert second.approved is False
    assert second.reason == "approval already consumed"


async def test_denied_tool_is_not_executed() -> None:
    calls = 0

    async def dangerous(arguments, context) -> str:
        nonlocal calls
        calls += 1
        return "executed"

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            ToolSpec(
                "dangerous",
                "Dangerous action",
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                ToolCapabilities(
                    filesystem="workspace-write",
                    subprocess=True,
                    risk_level=RiskLevel.HIGH,
                    side_effects=True,
                    idempotent=False,
                ),
            ),
            dangerous,
        )
    )
    provider = ScriptedModel(
        (
            (
                ToolCallDelta(0, "call-1", "dangerous", "{}"),
                ResponseCompleted("tool_calls"),
            ),
            (TextDelta("denied safely"), ResponseCompleted("stop")),
        )
    )
    loop = AgentLoop(
        provider,
        "fake",
        tools=registry,
        permission_policy=DefaultPermissionPolicy(),
        approval_provider=InMemoryApprovalProvider({}),
    )

    result = await loop.run("do it")

    assert calls == 0
    assert result.messages[2].content[0].is_error is True
    assert result.messages[2].content[0].output == "permission denied"
    assert any(isinstance(event.payload, ApprovalRequested) for event in result.events)
    assert any(isinstance(event.payload, ApprovalResolved) for event in result.events)


async def test_schema_error_reaches_model_without_requesting_approval(tmp_path) -> None:
    registry = ToolRegistry()
    for tool in make_file_tools():
        registry.register(tool)
    provider = ScriptedModel((
        (ToolCallDelta(0, "call-1", "write_file", '{"content":"text"}'), ResponseCompleted("tool_calls")),
        (TextDelta("done"), ResponseCompleted("stop")),
    ))
    result = await AgentLoop(
        provider, "fake", tools=registry, workspace=tmp_path,
        permission_policy=DefaultPermissionPolicy(),
    ).run("write a file")

    tool_result = provider.requests[1].messages[-1].content[0]
    assert isinstance(tool_result, ToolResultContent)
    assert tool_result.tool_call_id == "call-1" and tool_result.is_error
    assert "$.path: missing required property" in tool_result.output
    assert not any(isinstance(event.payload, ApprovalRequested) for event in result.events)
    assert not list(tmp_path.iterdir())

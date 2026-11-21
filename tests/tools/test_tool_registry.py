
import pytest

from miniclaw.core.errors import ToolNotFound
from miniclaw.core.messages import ToolCall
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import (
    RegisteredTool,
    ToolCapabilities,
    ToolContext,
    ToolSpec,
)


async def handler(arguments, context: ToolContext) -> str:
    return str(arguments["path"])


def make_tool() -> RegisteredTool:
    return RegisteredTool(
        ToolSpec(
            name="read_file",
            description="Read one file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            capabilities=ToolCapabilities(filesystem="read"),
        ),
        handler,
    )


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(make_tool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_tool())


def test_registry_exposes_openai_specs_and_lookup() -> None:
    registry = ToolRegistry()
    registry.register(make_tool())

    assert registry.get("read_file") is make_tool() or registry.get("read_file").spec.name == "read_file"
    assert registry.specs()[0]["type"] == "function"
    with pytest.raises(ToolNotFound):
        registry.get("missing")


async def test_disabled_tools_stay_disabled_in_subsets(tmp_path) -> None:
    async def must_not_run(arguments, context):
        pytest.fail("disabled handler was dispatched")

    tool = make_tool()
    registry = ToolRegistry()
    registry.register(RegisteredTool(tool.spec, must_not_run))
    registry.disable("read_file", "test backend unavailable")
    child = registry.subset(("read_file",))

    assert registry.specs() == child.specs() == ()
    result = await ToolExecutor(child).execute(
        ToolCall("stale", "read_file", {"path": "anything"}),
        ToolContext("run", "session", tmp_path),
    )
    assert result.is_error
    assert result.output == "tool unavailable: test backend unavailable"

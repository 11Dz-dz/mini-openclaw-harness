from miniclaw.core.errors import ToolArgumentsInvalid, ToolNotFound
from miniclaw.core.messages import ToolCall, ToolResultContent
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.schema import validate_arguments
from miniclaw.tools.types import ToolContext
from miniclaw.sandbox.router import SandboxRouter


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        sandbox_router: SandboxRouter | None = None,
    ) -> None:
        self.registry = registry
        self.sandbox_router = sandbox_router

    async def execute(
        self,
        call: ToolCall,
        context: ToolContext,
    ) -> ToolResultContent:
        try:
            tool = self.registry.get(call.name)
            arguments = validate_arguments(tool.spec.parameters, call.arguments)
            output = await tool.handler(arguments, context)
        except ToolNotFound:
            return ToolResultContent(call.id, "tool not found", is_error=True)
        except ToolArgumentsInvalid:
            return ToolResultContent(call.id, "tool arguments invalid", is_error=True)
        except Exception:
            return ToolResultContent(call.id, "tool execution failed", is_error=True)
        return ToolResultContent(call.id, output)

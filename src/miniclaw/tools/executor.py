import logging

from miniclaw.core.errors import ToolArgumentsInvalid, ToolNotFound
from miniclaw.core.messages import ToolCall, ToolResultContent
from miniclaw.sandbox.router import SandboxRouter
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.schema import validate_arguments
from miniclaw.tools.types import ToolContext

logger = logging.getLogger(__name__)

MAX_REASON_CHARACTERS = 300


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
            if (reason := self.registry.disabled_reason(call.name)) is not None:
                return ToolResultContent(
                    call.id, f"tool unavailable: {reason}", is_error=True
                )
            arguments = validate_arguments(tool.spec.parameters, call.arguments)
            output = await tool.handler(arguments, context)
            execution_id = None
            if self.registry.execution_recorder is not None:
                execution_id = await self.registry.execution_recorder(call, output, context)
        except ToolNotFound:
            return ToolResultContent(call.id, "tool not found", is_error=True)
        except ToolArgumentsInvalid:
            return ToolResultContent(call.id, "tool arguments invalid", is_error=True)
        except Exception as error:
            # The operator gets the traceback; the model gets only what is safe
            # to show it. Returning a bare "failed" to both made production
            # failures impossible to diagnose.
            logger.error(
                "tool %s failed in run %s (%s)", call.name, context.run_id,
                type(error).__name__,
            )
            return ToolResultContent(
                call.id,
                f"tool execution failed: {_describe(error)}",
                is_error=True,
            )
        return ToolResultContent(call.id, output, execution_id=execution_id)


def _describe(error: Exception) -> str:
    """Summarize a failure without leaking incidental internals.

    Tools raise ValueError deliberately to tell the caller what was wrong with
    a request, so that message is worth forwarding: it is how the model
    corrects itself. Any other exception is unplanned and its message may
    carry paths or payloads, so only the type is named.
    """
    if isinstance(error, ValueError):
        message = " ".join(str(error).split())
        if message:
            return message[:MAX_REASON_CHARACTERS]
    return type(error).__name__

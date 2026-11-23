import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from miniclaw.agent.events import ApprovalRequested, ApprovalResolved
from miniclaw.core.errors import ToolArgumentsInvalid, ToolNotFound
from miniclaw.core.messages import ToolCall, ToolResultContent
from miniclaw.permissions.approval import InMemoryApprovalProvider
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.preview import arguments_preview, canonical_arguments
from miniclaw.permissions.types import PermissionDecision, PermissionRequest
from miniclaw.tools.executor import ToolExecutor, argument_error_result
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.schema import validate_arguments
from miniclaw.tools.types import ToolContext

type ToolDecision = tuple[ToolCall, ToolResultContent | None]


async def prepare_tool_batch(
    calls: tuple[ToolCall, ...],
    registry: ToolRegistry | None,
    permission_policy: DefaultPermissionPolicy | None,
    approval_provider: InMemoryApprovalProvider | None,
    run_id: str,
    begin_approval: Callable[[], None],
    emit: Callable[[object], Awaitable[None]],
) -> tuple[ToolDecision, ...]:
    decisions: list[ToolDecision] = []
    for call in calls:
        frozen_arguments = canonical_arguments(call.arguments)
        # Keep the execution snapshot independent of the objects used for policy evaluation.
        frozen_call = ToolCall(call.id, call.name, json.loads(frozen_arguments))
        rejection = None
        registered = None
        if registry is not None and registry.disabled_reason(call.name) is None:
            with contextlib.suppress(ToolNotFound):
                registered = registry.get(call.name)
        if permission_policy is not None and registered is not None:
            try:
                validate_arguments(registered.spec.parameters, frozen_call.arguments)
            except ToolArgumentsInvalid as error:
                decisions.append((frozen_call, argument_error_result(call.id, error)))
                continue
            evaluation = permission_policy.evaluate(
                tool_name=call.name,
                capabilities=registered.spec.capabilities,
                arguments=json.loads(frozen_arguments),
            )
            if evaluation.decision is PermissionDecision.DENY:
                rejection = ToolResultContent(call.id, "permission denied", is_error=True)
            elif evaluation.decision is PermissionDecision.ASK:
                begin_approval()
                request_id = str(uuid4())
                request = PermissionRequest(
                    request_id, run_id, call.id, call.name,
                    arguments_preview(frozen_call.arguments),
                    registered.spec.capabilities, evaluation.reason,
                )
                await emit(ApprovalRequested(request_id, call.id, call.name, evaluation.reason))
                if approval_provider is None:
                    approved = False
                    reason = "approval provider unavailable"
                else:
                    resolution = await approval_provider.resolve(request)
                    approved = resolution.request_id == request_id and resolution.approved
                    reason = resolution.reason
                await emit(ApprovalResolved(request_id, approved, reason))
                if not approved:
                    rejection = ToolResultContent(call.id, "permission denied", is_error=True)
        decisions.append((frozen_call, rejection))
    return tuple(decisions)


async def execute_tool_batch(
    decisions: tuple[ToolDecision, ...], executor: ToolExecutor, context: ToolContext,
) -> AsyncIterator[tuple[ToolCall, ToolResultContent]]:
    for call, rejection in decisions:
        # Yield to the caller so it can checkpoint before dispatching the next call.
        result = rejection if rejection is not None else await executor.execute(call, context)
        yield call, result

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from miniclaw.agent.events import (
    ApprovalRequested as ApprovalRequested,
)
from miniclaw.agent.events import (
    ApprovalResolved as ApprovalResolved,
)
from miniclaw.agent.events import (
    ArtifactsReady,
    CompletionChecked,
    ContextBuilt,
    ContextInjected,
    ModelStreamStarted,
    ReasoningRecovery,
    RunCancelled,
    RunCompleted,
    RunEnded,
    RunFailed,
    RunStarted,
    VerificationStalled,
    WorkspaceVerified,
)
from miniclaw.agent.events import (
    ModelResponseFinished as ModelResponseFinished,
)
from miniclaw.agent.events import (
    ModelUsage as ModelUsage,
)
from miniclaw.agent.limits import RunLimits
from miniclaw.agent.request_context import build_request_context
from miniclaw.agent.state import RunStatus, transition
from miniclaw.agent.stream import receive_model_turn
from miniclaw.agent.tool_batch import execute_tool_batch, prepare_tool_batch
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.types import (
    AgentContext,
    ContextBudget,
    ContextBudgetExceeded,
    ContextSource,
)
from miniclaw.core.errors import ToolArgumentsInvalid
from miniclaw.core.events import EventEnvelope
from miniclaw.core.messages import (
    Message,
    ModelRequest,
    TextContent,
    ToolCall,
)
from miniclaw.model.base import ModelProvider
from miniclaw.observability.trace import TraceSink
from miniclaw.permissions.approval import InMemoryApprovalProvider
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.preview import canonical_arguments
from miniclaw.sessions.checkpoints import FileCheckpointStore
from miniclaw.sessions.database import SessionRepository
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.sessions.recovery import close_interrupted_tool_calls
from miniclaw.sessions.types import Checkpoint, RunRecord, SessionRecord
from miniclaw.tools.assembler import ToolCallAssembler
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RegisteredTool, ToolCapabilities, ToolContext, ToolSpec
from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path

MAX_TOOL_CALL_CORRECTIONS = 2
MAX_COMPLETION_CORRECTIONS = 2
REASONING_SPIN_LIMIT = 2
REASONING_RECOVERY_TOOLS = {"write_file", "run_command"}
REASONING_COOLDOWN_TURNS = 4
REASONING_LATCH_THRESHOLD = 2
BUDGET_NOTICE_TURNS = 4
type CompletionCheck = Callable[[Path], Awaitable[str | None]]
OUTPUT_TRUNCATION_CORRECTION = ContextSource(
    "model:output-truncation",
    "The previous reply was cut off at the output token limit. None of its tool "
    "calls ran. Preserve earlier completed work. Use a short response and issue "
    "one small, complete tool call. Split large edits into smaller steps.",
    priority=100,
    required=True,
)
TOOL_CALL_CORRECTION = ContextSource(
    "model:tool-call-correction",
    "The previous reply contained an invalid tool call batch. None of its calls ran. "
    "Continue the task by issuing only the calls you still need, with a complete "
    "JSON arguments object for each call.",
    priority=100,
    required=True,
)
REASONING_RECOVERY = ContextSource(
    "model:reasoning-recovery",
    "The output limit was reached repeatedly before a tool call could start. Thinking is temporarily disabled. "
    "Use one small workspace tool call now to write or run an initial step. Break the task into steps and use runtime "
    "feedback before attempting the full solution.",
    priority=100,
    required=True,
)
CODING_GUIDANCE = ContextSource(
    "coding:guidance",
    "Use search_text to locate the relevant code and stop broad exploration once you have a workable approach. "
    "Write an initial patch or deliverable early. If verify_workspace is available, call it on the initial deliverable "
    "to establish a baseline before writing another evaluation script. Prefer replace_text for small edits to existing "
    "files, and run a focused test after each meaningful change.",
    priority=100,
    required=True,
)
ARTIFACTS_READY_FEEDBACK = ContextSource(
    "workspace:artifacts-ready",
    "The required deliverables now exist. Call verify_workspace next to run the configured checks, "
    "then use the results to decide what to improve.",
    priority=100,
    required=True,
)


class ContextProvider(Protocol):
    """Contribute extra context sources for one run, e.g. skills or memory."""

    async def build(self, prompt: str) -> tuple[ContextSource, ...]: ...


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    session_id: str
    status: RunStatus
    output: str
    messages: tuple[Message, ...]
    events: tuple[EventEnvelope, ...]


def _missing_artifacts(workspace: Path, required: tuple[str, ...]) -> tuple[str, ...]:
    missing = []
    for relative in required:
        try:
            if resolve_workspace_path(workspace, relative).is_file():
                continue
        except (WorkspaceEscape, OSError, RuntimeError):
            pass
        missing.append(relative)
    return tuple(missing)


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        *,
        limits: RunLimits | None = None,
        tools: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        workspace: Path | None = None,
        session_repository: SessionRepository | None = None,
        checkpoint_store: FileCheckpointStore | None = None,
        event_store: JsonlEventStore | None = None,
        clock: Callable[[], datetime] | None = None,
        context_builder: ContextBuilder | None = None,
        context_budget: ContextBudget | None = None,
        permission_policy: DefaultPermissionPolicy | None = None,
        approval_provider: InMemoryApprovalProvider | None = None,
        trace_sinks: tuple[TraceSink, ...] = (),
        context_providers: tuple["ContextProvider", ...] = (),
        strip_previous_turn_reasoning: bool = False,
        reasoning_circuit_breaker: bool = False,
        coding_guidance: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.limits = limits or RunLimits()
        self.tools = tools
        self.tool_executor = tool_executor or (
            ToolExecutor(tools) if tools is not None else None
        )
        self.workspace = workspace
        self.session_repository = session_repository
        self.checkpoint_store = checkpoint_store
        self.event_store = event_store
        self.clock = clock or (lambda: datetime.now(UTC))
        if (context_builder is None) != (context_budget is None):
            raise ValueError(
                "context_builder and context_budget must be configured together"
            )
        self.context_builder = context_builder
        self.context_budget = context_budget
        self.permission_policy = permission_policy
        self.approval_provider = approval_provider
        self.trace_sinks = trace_sinks
        self.context_providers = context_providers
        self.strip_previous_turn_reasoning = strip_previous_turn_reasoning
        self.reasoning_circuit_breaker = reasoning_circuit_breaker
        self.coding_guidance = coding_guidance

    async def run(
        self,
        prompt: str,
        *,
        history: tuple[Message, ...] = (),
        session_id: str | None = None,
        trace_sinks: tuple[TraceSink, ...] = (),
        required_artifacts: tuple[str, ...] = (),
        completion_check: CompletionCheck | None = None,
        force_artifact_tool: bool = False,
        active_verification: bool = False,
        pivot_on_stall: bool = False,
    ) -> RunResult:
        run_id = str(uuid4())
        session_id = session_id or str(uuid4())
        events: list[EventEnvelope] = []
        messages = [*history, Message("user", (TextContent(prompt),))]
        previous_message_count = len(history)
        status = RunStatus.CREATED

        completed_tool_executions: list[str] = []
        context_snapshot: AgentContext | None = None
        terminal_emitted = False

        def begin_approval() -> None:
            nonlocal status
            if status is RunStatus.VALIDATING_TOOLS:
                status = transition(status, RunStatus.AWAITING_APPROVAL)

        async def emit(payload: object) -> None:
            envelope = EventEnvelope.create(
                run_id=run_id,
                session_id=session_id,
                sequence=len(events) + 1,
                payload=payload,
                parent_event_id=events[-1].event_id if events else None,
            )
            events.append(envelope)
            if self.event_store is not None:
                await self.event_store.append(envelope)
            for sink in (*self.trace_sinks, *trace_sinks):
                await sink.emit(envelope)

        async def checkpoint() -> None:
            nonlocal terminal_emitted
            if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                          RunStatus.EXHAUSTED} and not terminal_emitted:
                terminal_emitted = True
                await emit(RunEnded(status.value))
            if self.session_repository is not None:
                await self.session_repository.set_run_status(run_id, status.value)
            if self.checkpoint_store is None:
                return
            await self.checkpoint_store.save(
                Checkpoint(
                    checkpoint_id=str(uuid4()),
                    run_id=run_id,
                    session_id=session_id,
                    sequence=len(events),
                    status=status.value,
                    messages=tuple(messages),
                    completed_tool_executions=tuple(completed_tool_executions),
                    context_snapshot=context_snapshot,
                )
            )

        last_check_error: str | None = None
        same_failed_checks = 0
        consecutive_failed_checks = 0
        completion_feedback: list[ContextSource] = []
        verification_registered = active_verification and completion_check is not None
        ready_artifacts_checked = False

        async def check_workspace(workspace: Path, *, on_demand: bool) -> str | None:
            nonlocal last_check_error, same_failed_checks, consecutive_failed_checks
            assert completion_check is not None
            error = await completion_check(workspace)
            await emit(WorkspaceVerified(error is None) if on_demand else CompletionChecked(error is None))
            if error is None:
                completion_feedback.clear()
            was_stalled = same_failed_checks >= 2 or consecutive_failed_checks >= 3
            same_failed_checks = ((same_failed_checks + 1 if error == last_check_error else 1)
                                  if error is not None else 0)
            consecutive_failed_checks = consecutive_failed_checks + 1 if error is not None else 0
            last_check_error = error
            if error is not None and pivot_on_stall and (same_failed_checks >= 2 or consecutive_failed_checks >= 3):
                if not was_stalled:
                    await emit(VerificationStalled(consecutive_failed_checks))
                reason = ("The check returned the same failure twice. " if same_failed_checks >= 2
                          else f"The workspace has failed {consecutive_failed_checks} consecutive checks. ")
                return (error + " " + reason + "Choose a different approach "
                        "that targets a failing constraint. Make one focused change and check its effect.")
            return error

        registry, executor = self.tools, self.tool_executor
        if verification_registered:
            async def verify_tool(_arguments, context):
                nonlocal ready_artifacts_checked
                if required_artifacts and not _missing_artifacts(context.workspace, required_artifacts):
                    ready_artifacts_checked = True
                error = await check_workspace(context.workspace, on_demand=True)
                return error if error is not None else "The configured workspace check passed."

            registry = (registry.subset(tuple(tool.spec.name for tool in registry.values()))
                        if registry is not None else ToolRegistry())
            registry.register(RegisteredTool(ToolSpec(
                "verify_workspace",
                "Run the configured read-only delivery check on the current workspace. "
                "Use it after creating or changing deliverables to compare results before completion.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                ToolCapabilities(filesystem="read", subprocess=True),
            ), verify_tool))
            executor = ToolExecutor(registry, executor.sandbox_router if executor is not None else None)

        if self.session_repository is not None:
            now = self.clock()
            if await self.session_repository.get_session(session_id) is None:
                await self.session_repository.create_session(
                    SessionRecord(session_id, now, now)
                )
            else:
                await self.session_repository.touch_session(session_id, now)
            await self.session_repository.create_run(
                RunRecord(run_id, session_id, status.value, now, now)
            )

        try:
            await emit(RunStarted(prompt))
            if self.limits.max_turns <= 0:
                status = transition(status, RunStatus.BUILDING_CONTEXT)
                await emit(ContextBuilt(len(messages)))
                await checkpoint()
                status = transition(status, RunStatus.CALLING_MODEL)
                status = transition(status, RunStatus.EXHAUSTED)
                await checkpoint()
                return RunResult(
                    run_id,
                    session_id,
                    status,
                    "",
                    tuple(messages),
                    tuple(events),
                )

            extra_sources: tuple[ContextSource, ...] = ()
            for provider in self.context_providers:
                extra_sources += await provider.build(prompt)
            extra_sources = tuple(
                source for source in extra_sources if source.text.strip()
            )
            if self.coding_guidance:
                extra_sources = (CODING_GUIDANCE, *extra_sources)

            tool_calls_used = 0
            tool_call_corrections = 0
            completion_corrections = 0
            output_truncation_retries = 0
            reasoning_spin_streak = 0
            reasoning_cooldown = 0
            reasoning_recovery_count = 0
            next_feedback: ContextSource | None = None
            workspace = self.workspace or Path.cwd()
            artifacts_ready = not required_artifacts or not _missing_artifacts(workspace, required_artifacts)
            remind_artifacts_ready = artifacts_ready and bool(required_artifacts) and verification_registered
            if artifacts_ready and required_artifacts:
                await emit(ArtifactsReady(0))
            for turn in range(1, self.limits.max_turns + 1):
                if status in {
                    RunStatus.RECORDING_RESULTS,
                    RunStatus.RECORDING_TOOL_ERROR,
                } or status is RunStatus.CREATED:
                    status = transition(status, RunStatus.BUILDING_CONTEXT)
                request_messages = tuple(messages)
                if self.strip_previous_turn_reasoning:
                    request_messages = (*(
                        replace(message, reasoning_content=None) if message.reasoning_content is not None else message
                        for message in messages[:previous_message_count]
                    ), *messages[previous_message_count:])
                tools = registry.specs() if registry else ()
                tool_choice = "required" if (force_artifact_tool and tools and next_feedback is not None
                                             and next_feedback.source_id == "model:missing-artifacts") else None
                recovery_tools = (tuple(spec for spec in tools if spec["function"]["name"] in REASONING_RECOVERY_TOOLS)
                                  if reasoning_spin_streak >= REASONING_SPIN_LIMIT else ())
                active_feedback = list(completion_feedback)
                if next_feedback is not None:
                    if not active_feedback or active_feedback[-1] != next_feedback:
                        active_feedback.append(next_feedback)
                    next_feedback = None
                if recovery_tools:
                    tools = recovery_tools
                    tool_choice = "required"
                    if not active_feedback or active_feedback[-1] != REASONING_RECOVERY:
                        active_feedback.append(REASONING_RECOVERY)
                thinking_disabled = bool(recovery_tools or reasoning_cooldown
                                         or reasoning_recovery_count >= REASONING_LATCH_THRESHOLD)
                reasoning_cooldown = max(0, reasoning_cooldown - 1)
                feedback = active_feedback[-2:]
                if remind_artifacts_ready and not recovery_tools:
                    feedback.append(ARTIFACTS_READY_FEEDBACK)
                    remind_artifacts_ready = False
                remaining_turns = self.limits.max_turns - turn + 1
                if remaining_turns <= BUDGET_NOTICE_TURNS:
                    count = "only 1 turn remains" if remaining_turns == 1 else f"only {remaining_turns} turns remain"
                    feedback.append(ContextSource(
                        "model:budget-notice", f"Budget notice: {count}, including this one. "
                        "Stop broad exploration, write/update the required deliverable or patch immediately, "
                        "and run a focused check.", priority=100, required=True,
                    ))
                feedback_text = "\n".join(f"- {item.text}" for item in feedback)
                feedback_message = (Message("user", (TextContent(
                    "[Harness follow-up for the current task]\n" + feedback_text),)) if feedback_text else None)
                # Budget the tail separately so the original task stays required.
                round_sources = extra_sources
                if feedback_message is not None:
                    round_sources = (*round_sources, ContextSource(
                        "harness:feedback-tail", feedback_text, priority=100, required=True))
                request, context_snapshot, head_sources = build_request_context(
                    ModelRequest(
                        self.model, request_messages, tools,
                        max_output_tokens=(self.context_budget.reserve_output
                                           if self.context_budget else None),
                        tool_choice=tool_choice,
                        enable_thinking=False if thinking_disabled else None,
                    ),
                    round_sources, feedback_message, self.context_builder, self.context_budget,
                )
                injected = tuple(source.source_id for source in head_sources) + tuple(
                    dict.fromkeys(source.source_id for source in feedback))
                if injected:
                    await emit(ContextInjected(injected))
                await emit(
                    ContextBuilt(
                        len(request.messages),
                        (
                            context_snapshot.estimated_tokens
                            if context_snapshot is not None
                            else None
                        ),
                        (
                            context_snapshot.dropped_source_ids
                            if context_snapshot is not None
                            else ()
                        ),
                    )
                )
                await checkpoint()
                status = transition(status, RunStatus.CALLING_MODEL)
                await emit(ModelStreamStarted(turn))
                if recovery_tools:
                    await emit(ReasoningRecovery(turn, recovered=False))
                assembler = ToolCallAssembler()
                available_tool_names = {spec["function"]["name"] for spec in tools}
                response = await receive_model_turn(
                    self.provider, request, assembler, available_tool_names, turn, emit,
                )
                finish_reason = response.finish_reason
                answer = "".join(response.answer)
                reasoning = "".join(response.reasoning) if response.reasoning else None
                if self.reasoning_circuit_breaker and not thinking_disabled:
                    if (finish_reason == "length" and not response.tool_fragments
                            and (response.reasoning or response.reasoning_tokens)):
                        reasoning_spin_streak += 1
                    else:
                        reasoning_spin_streak = 0
                failure_reason = "model did not complete a usable response"
                if finish_reason == "stop" and recovery_tools:
                    failure_reason = "reasoning recovery requires a successful workspace tool call"
                    if turn < self.limits.max_turns and tool_call_corrections < MAX_TOOL_CALL_CORRECTIONS:
                        tool_call_corrections += 1
                        next_feedback = REASONING_RECOVERY
                elif finish_reason == "stop":
                    missing = _missing_artifacts(workspace, required_artifacts)
                    completion_error = None
                    source_id = "model:missing-artifacts"
                    if missing:
                        completion_error = f"required workspace files missing: {json.dumps(missing)}"
                    elif completion_check is not None:
                        completion_error = await check_workspace(workspace, on_demand=False)
                        source_id = "model:verification-failed"
                    if completion_error is not None:
                        failure_reason = completion_error
                        if (turn < self.limits.max_turns and tool_calls_used < self.limits.max_tool_calls
                                and completion_corrections < MAX_COMPLETION_CORRECTIONS):
                            completion_corrections += 1
                            next_feedback = ContextSource(
                                source_id,
                                f"Check {completion_corrections} failed. {failure_reason}. "
                                + ("Use a file or command tool now to create the missing required files. " if missing
                                   else "Use a workspace tool now to inspect or change the failing deliverable. ")
                                + "Continue from the current workspace and run a focused check before a final answer.",
                                priority=100,
                                required=True,
                            )
                            completion_feedback.append(next_feedback)
                    else:
                        output = answer
                        messages.append(Message("assistant", (TextContent(output),),
                                                reasoning))
                        status = transition(status, RunStatus.COMPLETED)
                        await emit(RunCompleted(output))
                        await checkpoint()
                        return RunResult(
                            run_id, session_id, status, output, tuple(messages), tuple(events),
                        )
                elif finish_reason == "length":
                    failure_reason = "model response exceeded its output token limit"
                    if (turn < self.limits.max_turns
                            and output_truncation_retries < self.limits.max_output_truncation_retries):
                        output_truncation_retries += 1
                        detail = (" Interrupted tools: " + ", ".join(sorted(response.pending_tool_names)) + "."
                                  if response.pending_tool_names else
                                  " No tool started. Make one focused tool call before further analysis.")
                        next_feedback = replace(OUTPUT_TRUNCATION_CORRECTION,
                                                text=OUTPUT_TRUNCATION_CORRECTION.text + detail)
                if next_feedback is not None:
                    status = transition(status, RunStatus.BUILDING_CONTEXT)
                    continue
                if finish_reason != "tool_calls" or executor is None:
                    status = transition(status, RunStatus.FAILED)
                    await emit(RunFailed(failure_reason))
                    await checkpoint()
                    return RunResult(
                        run_id,
                        session_id,
                        status,
                        failure_reason if finish_reason in {"length", "stop"} else answer,
                        tuple(messages),
                        tuple(events),
                    )
                try:
                    if response.assembly_error is not None:
                        raise response.assembly_error
                    calls = tuple(ToolCall(call.id, call.name,
                                  json.loads(canonical_arguments(call.arguments)))
                                  for call in assembler.complete())
                    if not calls:
                        raise ToolArgumentsInvalid("model reported tool calls without any calls")
                    if recovery_tools and any(call.name not in available_tool_names for call in calls):
                        raise ToolArgumentsInvalid("reasoning recovery only accepts the tools offered for this request")
                except (ToolArgumentsInvalid, ValueError) as error:
                    if (tool_call_corrections >= MAX_TOOL_CALL_CORRECTIONS
                            or turn >= self.limits.max_turns):
                        raise ToolArgumentsInvalid("tool call correction limit reached") from error
                    tool_call_corrections += 1
                    next_feedback = TOOL_CALL_CORRECTION
                    status = transition(status, RunStatus.BUILDING_CONTEXT)
                    continue
                if tool_calls_used + len(calls) > self.limits.max_tool_calls:
                    status = transition(status, RunStatus.EXHAUSTED)
                    await checkpoint()
                    return RunResult(
                        run_id,
                        session_id,
                        status,
                        answer,
                        tuple(messages),
                        tuple(events),
                    )
                status = transition(status, RunStatus.VALIDATING_TOOLS)
                assistant_content = ((TextContent(answer),) if response.answer else ()) + calls
                messages.append(Message("assistant", assistant_content,
                                        reasoning))
                await checkpoint()
                context = ToolContext(
                    run_id, session_id, self.workspace or Path.cwd(), request_text=prompt,
                )
                decisions = await prepare_tool_batch(
                    calls, registry, self.permission_policy, self.approval_provider,
                    run_id, begin_approval, emit,
                )
                any_allowed = any(rejection is None for _, rejection in decisions)
                target = (
                    RunStatus.EXECUTING_TOOLS
                    if any_allowed
                    else RunStatus.RECORDING_TOOL_ERROR
                )
                status = transition(status, target)
                recovered = False
                async for call, result in execute_tool_batch(decisions, executor, context):
                    tool_calls_used += 1
                    # Persist each outcome before dispatching the next tool.
                    messages.append(Message("tool", (result,)))
                    completed_tool_executions.append(result.tool_call_id)
                    await checkpoint()
                    if recovery_tools and call.name in REASONING_RECOVERY_TOOLS and not result.is_error:
                        recovered = True
                if recovered:
                    reasoning_spin_streak = 0
                    reasoning_cooldown = REASONING_COOLDOWN_TURNS
                    reasoning_recovery_count += 1
                    await emit(ReasoningRecovery(turn, recovered=True))
                if status is RunStatus.EXECUTING_TOOLS:
                    status = transition(status, RunStatus.RECORDING_RESULTS)
                if not artifacts_ready and not _missing_artifacts(workspace, required_artifacts):
                    artifacts_ready = True
                    await emit(ArtifactsReady(turn))
                    remind_artifacts_ready = verification_registered and not ready_artifacts_checked
            status = transition(status, RunStatus.EXHAUSTED)
            await checkpoint()
            return RunResult(
                run_id,
                session_id,
                status,
                "",
                tuple(messages),
                tuple(events),
            )
        except asyncio.CancelledError:
            status = transition(status, RunStatus.CANCELLED)
            await emit(RunCancelled())
            await checkpoint()
            raise
        except Exception as error:
            status = RunStatus.FAILED
            reason = (str(error) if isinstance(error, ContextBudgetExceeded)
                      else f"run failed: {getattr(error, 'code', type(error).__name__)}")
            messages = list(close_interrupted_tool_calls(tuple(messages)))
            with contextlib.suppress(Exception):
                await emit(RunFailed(reason))
            with contextlib.suppress(Exception):
                await checkpoint()
            if self.session_repository is not None:
                with contextlib.suppress(Exception):
                    await self.session_repository.set_run_status(run_id, status.value)
            return RunResult(run_id, session_id, status, reason,
                             tuple(messages), tuple(events))

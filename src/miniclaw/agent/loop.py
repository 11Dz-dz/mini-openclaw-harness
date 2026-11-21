import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from miniclaw.agent.limits import RunLimits
from miniclaw.agent.state import RunStatus, transition
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.history import compact_dropped_history, select_request_history
from miniclaw.context.types import (
    AgentContext,
    ContextBudget,
    ContextBudgetExceeded,
    ContextSource,
)
from miniclaw.core.errors import ToolArgumentsInvalid, ToolNotFound
from miniclaw.core.events import EventEnvelope
from miniclaw.core.messages import (
    Message,
    ModelRequest,
    ReasoningDelta,
    ResponseCompleted,
    TextContent,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolResultContent,
)
from miniclaw.model.base import ModelProvider
from miniclaw.model.encoding import encode_message, encode_request
from miniclaw.observability.trace import TraceSink
from miniclaw.permissions.approval import InMemoryApprovalProvider
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.preview import arguments_preview, canonical_arguments
from miniclaw.permissions.types import PermissionDecision, PermissionRequest
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


@dataclass(frozen=True, slots=True)
class RunStarted:
    prompt: str


@dataclass(frozen=True, slots=True)
class ContextBuilt:
    message_count: int
    estimated_tokens: int | None = None
    dropped_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelStreamStarted:
    turn: int


@dataclass(frozen=True, slots=True)
class ModelResponseFinished:
    turn: int
    reason: str
    reasoning_tokens: int | None
    reasoning_chars: int
    visible_chars: int
    tool_fragments: int


@dataclass(frozen=True, slots=True)
class ArtifactsReady:
    turn: int


@dataclass(frozen=True, slots=True)
class ReasoningRecovery:
    turn: int
    recovered: bool


@dataclass(frozen=True, slots=True)
class RunCompleted:
    output: str


@dataclass(frozen=True, slots=True)
class CompletionChecked:
    passed: bool


@dataclass(frozen=True, slots=True)
class WorkspaceVerified:
    passed: bool


@dataclass(frozen=True, slots=True)
class VerificationStalled:
    consecutive_checks: int


@dataclass(frozen=True, slots=True)
class RunCancelled:
    reason: str = "cancelled"


@dataclass(frozen=True, slots=True)
class RunFailed:
    reason: str


@dataclass(frozen=True, slots=True)
class RunEnded:
    status: str


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    request_id: str
    tool_call_id: str
    tool_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    request_id: str
    approved: bool
    reason: str


class ContextProvider(Protocol):
    """Contribute extra context sources for one run, e.g. skills or memory."""

    async def build(self, prompt: str) -> tuple[ContextSource, ...]: ...


@dataclass(frozen=True, slots=True)
class ContextInjected:
    source_ids: tuple[str, ...]


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


def _recovery_tool_succeeded(call: ToolCall, result: ToolResultContent) -> bool:
    if call.name not in REASONING_RECOVERY_TOOLS or result.is_error:
        return False
    if call.name == "write_file":
        return True
    try:
        output = json.loads(result.output)
    except ValueError:
        return False
    return isinstance(output, dict) and type(output.get("exit_code")) is int and output["exit_code"] == 0


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
        completion_feedback: list[ContextSource] = []

        async def check_workspace(workspace: Path, *, on_demand: bool) -> str | None:
            nonlocal last_check_error, same_failed_checks
            assert completion_check is not None
            error = await completion_check(workspace)
            await emit(WorkspaceVerified(error is None) if on_demand else CompletionChecked(error is None))
            if error is None:
                completion_feedback.clear()
            same_failed_checks = ((same_failed_checks + 1 if error == last_check_error else 1)
                                  if error is not None else 0)
            last_check_error = error
            if error is not None and pivot_on_stall and same_failed_checks >= 2:
                if same_failed_checks == 2:
                    await emit(VerificationStalled(same_failed_checks))
                return (error + " The check returned the same failure twice. Choose a different approach "
                        "that targets a failing constraint. Make one focused change and check its effect.")
            return error

        registry, executor = self.tools, self.tool_executor
        if active_verification and completion_check is not None:
            async def verify_tool(_arguments, context):
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

            tool_calls_used = 0
            tool_call_corrections = 0
            completion_corrections = 0
            output_truncation_retries = 0
            reasoning_spin_streak = 0
            next_feedback: ContextSource | None = None
            workspace = self.workspace or Path.cwd()
            artifacts_ready = not required_artifacts or not _missing_artifacts(workspace, required_artifacts)
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
                feedback_text = "\n".join(f"- {item.text}" for item in active_feedback[-2:])
                feedback_message = (Message("user", (TextContent(
                    "[Harness follow-up for the current task]\n" + feedback_text),)) if feedback_text else None)
                # Budget the tail separately so the original task stays required.
                round_sources = extra_sources
                if feedback_message is not None:
                    round_sources = (*round_sources, ContextSource(
                        "harness:feedback-tail", feedback_text, priority=100, required=True))
                selected_extra = round_sources
                if self.context_builder is not None and self.context_budget is not None:
                    overhead = self.context_builder.estimator.estimate(json.dumps(
                        encode_request(ModelRequest(
                            self.model, (), tools, max_output_tokens=self.context_budget.reserve_output,
                            tool_choice=tool_choice, enable_thinking=False if recovery_tools else None), stream=True),
                        ensure_ascii=False, separators=(",", ":")))
                    history = request_messages
                    budget_sources = tuple(replace(source, token_text=json.dumps(encode_message(
                            feedback_message if source.source_id == "harness:feedback-tail"
                            else Message("system", (TextContent(source.text),))),
                            ensure_ascii=False)) for source in round_sources)
                    context_snapshot, request_messages = select_request_history(
                        self.context_builder, history, budget_sources,
                        ContextBudget(self.context_budget.total - overhead,
                                      self.context_budget.reserve_output),
                    )
                    excerpt = compact_dropped_history(
                        history, set(context_snapshot.dropped_source_ids),
                        self.context_builder.estimator,
                        min(2048, self.context_budget.input_limit - overhead
                            - context_snapshot.estimated_tokens),
                    )
                    if excerpt is not None:
                        encoded = json.dumps(encode_message(Message('system', (TextContent(excerpt.text),))),
                                             ensure_ascii=False)
                        excerpt_cost = self.context_builder.estimator.estimate(encoded)
                        if (context_snapshot.estimated_tokens + excerpt_cost + overhead
                                <= self.context_budget.input_limit):
                            context_snapshot = replace(context_snapshot,
                                items=(*context_snapshot.items, excerpt),
                                estimated_tokens=context_snapshot.estimated_tokens + excerpt_cost)
                    context_snapshot = replace(context_snapshot,
                        estimated_tokens=context_snapshot.estimated_tokens + overhead)
                    selected_indices = {
                        int(item.source_id.removeprefix("message:"))
                        for item in context_snapshot.items
                        if item.source_id.startswith("message:")
                    }
                    selected_extra = tuple(
                        item
                        for item in context_snapshot.items
                        if not item.source_id.startswith("message:")
                    )
                    request_messages = tuple(
                        message
                        for index, message in enumerate(request_messages)
                        if index in selected_indices
                    )
                head_sources = tuple(item for item in selected_extra if item.source_id != "harness:feedback-tail")
                if head_sources:
                    request_messages = (
                        Message(
                            "system",
                            tuple(
                            TextContent(item.text) for item in head_sources
                            ),
                        ),
                        *request_messages,
                    )
                if feedback_message is not None:
                    request_messages = (*request_messages, feedback_message)
                injected = tuple(source.source_id for source in head_sources) + tuple(
                    dict.fromkeys(source.source_id for source in active_feedback[-2:]))
                if injected:
                    await emit(ContextInjected(injected))
                await emit(
                    ContextBuilt(
                        len(request_messages),
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
                answer: list[str] = []
                reasoning: list[str] = []
                assembler = ToolCallAssembler()
                assembly_error: ToolArgumentsInvalid | None = None
                finish_reason: str | None = None
                reasoning_tokens: int | None = None
                tool_fragments = 0
                pending_tool_names: set[str] = set()
                available_tool_names = {spec["function"]["name"] for spec in tools}
                async for event in self.provider.stream(
                    ModelRequest(
                        self.model,
                        request_messages,
                        tools,
                        max_output_tokens=(self.context_budget.reserve_output
                                           if self.context_budget else None),
                        tool_choice=tool_choice,
                        enable_thinking=False if recovery_tools else None,
                    )
                ):
                    if isinstance(event, TextDelta):
                        answer.append(event.text)
                        await emit(event)
                    elif isinstance(event, ReasoningDelta):
                        # Preserve provider state for tool continuation without
                        # exposing it as an answer or recording it in audit streams.
                        reasoning.append(event.text)
                    elif isinstance(event, ToolCallDelta):
                        tool_fragments += 1
                        if event.name in available_tool_names:
                            pending_tool_names.add(event.name)
                        if assembly_error is None:
                            try:
                                assembler.feed(event)
                            except ToolArgumentsInvalid as error:
                                assembly_error = error
                        await emit(event)
                    elif isinstance(event, ResponseCompleted):
                        finish_reason = event.finish_reason
                        if event.usage is not None:
                            await emit(ModelUsage(event.usage.input_tokens, event.usage.output_tokens))
                            reasoning_tokens = event.usage.reasoning_tokens
                        await emit(ModelResponseFinished(
                            turn, finish_reason if finish_reason in {"stop", "length", "tool_calls", "content_filter"}
                            else "other", reasoning_tokens,
                            sum(map(len, reasoning)), sum(map(len, answer)), tool_fragments,
                        ))
                        break
                if self.reasoning_circuit_breaker and not recovery_tools:
                    if finish_reason == "length" and not tool_fragments and (reasoning or reasoning_tokens):
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
                        output = "".join(answer)
                        messages.append(Message("assistant", (TextContent(output),),
                                                "".join(reasoning) if reasoning else None))
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
                        detail = (" Interrupted tools: " + ", ".join(sorted(pending_tool_names)) + "."
                                  if pending_tool_names else
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
                        failure_reason if finish_reason in {"length", "stop"} else "".join(answer),
                        tuple(messages),
                        tuple(events),
                    )
                try:
                    if assembly_error is not None:
                        raise assembly_error
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
                        "".join(answer),
                        tuple(messages),
                        tuple(events),
                    )
                status = transition(status, RunStatus.VALIDATING_TOOLS)
                assistant_content = ((TextContent("".join(answer)),) if answer else ()) + calls
                messages.append(Message("assistant", assistant_content,
                                        "".join(reasoning) if reasoning else None))
                await checkpoint()
                decisions: list[tuple[object, bool]] = []
                context = ToolContext(
                    run_id,
                    session_id,
                    self.workspace or Path.cwd(),
                    request_text=prompt,
                )
                for call in calls:
                    frozen_arguments = canonical_arguments(call.arguments)
                    allowed = True
                    # Approval cannot provide a missing isolation capability.
                    # ToolExecutor refuses unavailable calls before dispatch.
                    unavailable = (
                        registry is not None
                        and registry.disabled_reason(call.name) is not None
                    )
                    registered = None
                    if registry is not None:
                        with contextlib.suppress(ToolNotFound):
                            registered = registry.get(call.name)
                    if (self.permission_policy is not None and not unavailable
                            and registered is not None):
                        evaluation = self.permission_policy.evaluate(
                            tool_name=call.name,
                            capabilities=registered.spec.capabilities,
                            arguments=call.arguments,
                        )
                        if evaluation.decision is PermissionDecision.DENY:
                            allowed = False
                        elif evaluation.decision is PermissionDecision.ASK:
                            if status is RunStatus.VALIDATING_TOOLS:
                                status = transition(
                                    status, RunStatus.AWAITING_APPROVAL
                                )
                            request_id = str(uuid4())
                            request = PermissionRequest(
                                request_id,
                                run_id,
                                call.id,
                                call.name,
                                arguments_preview(call.arguments),
                                registered.spec.capabilities,
                                evaluation.reason,
                            )
                            await emit(
                                ApprovalRequested(
                                    request_id,
                                    call.id,
                                    call.name,
                                    evaluation.reason,
                                )
                            )
                            if self.approval_provider is None:
                                allowed = False
                                resolution_reason = "approval provider unavailable"
                            else:
                                resolution = await self.approval_provider.resolve(
                                    request
                                )
                                allowed = (resolution.request_id == request_id
                                           and resolution.approved)
                                resolution_reason = resolution.reason
                            await emit(
                                ApprovalResolved(
                                    request_id,
                                    allowed,
                                    resolution_reason,
                                )
                            )
                    # Execute the exact reviewed snapshot, even if another
                    # object retained a reference to the mutable arguments.
                    decisions.append((ToolCall(call.id, call.name,
                                              json.loads(frozen_arguments)), allowed))
                any_allowed = any(allowed for _, allowed in decisions)
                target = (
                    RunStatus.EXECUTING_TOOLS
                    if any_allowed
                    else RunStatus.RECORDING_TOOL_ERROR
                )
                status = transition(status, target)
                recovered = False
                for call, allowed in decisions:
                    if allowed:
                        result = await executor.execute(call, context)
                    else:
                        result = ToolResultContent(call.id, "permission denied", is_error=True)
                    tool_calls_used += 1
                    # Persist each outcome before dispatching the next tool.
                    messages.append(Message("tool", (result,)))
                    completed_tool_executions.append(result.tool_call_id)
                    await checkpoint()
                    if recovery_tools and _recovery_tool_succeeded(call, result):
                        recovered = True
                if recovered:
                    reasoning_spin_streak = 0
                    await emit(ReasoningRecovery(turn, recovered=True))
                if status is RunStatus.EXECUTING_TOOLS:
                    status = transition(status, RunStatus.RECORDING_RESULTS)
                if not artifacts_ready and not _missing_artifacts(workspace, required_artifacts):
                    artifacts_ready = True
                    await emit(ArtifactsReady(turn))
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

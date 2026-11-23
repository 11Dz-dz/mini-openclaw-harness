from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from miniclaw.cli.commands import CommandResult
from miniclaw.observability.metrics import metrics_from_events
from miniclaw.permissions.preview import arguments_preview
from miniclaw.permissions.types import PermissionRequest
from miniclaw.sessions.recovery import (
    RecoveryDecision,
    ToolExecutionRecord,
    close_interrupted_tool_calls,
    decide_recovery,
    pending_tool_executions,
)
from miniclaw.sessions.serialization import PersistenceError
from miniclaw.sessions.types import Checkpoint, RunRecord
from miniclaw.tools.types import RiskLevel, ToolCapabilities
from miniclaw.workspace.paths import WorkspaceEscape

if TYPE_CHECKING:
    from miniclaw.application import MiniClawApp


async def handle_command(app: MiniClawApp, text: str) -> CommandResult:
    if text.split(maxsplit=1)[:1] == ["/skill"]:
        name = text.partition(' ')[2].strip().split(maxsplit=1)[:1]
        if not name:
            return CommandResult(True, "usage: /skill <name> [request]")
        if name[0] not in {skill.name for skill in app.skills}:
            return CommandResult(True, f"skill not found: {name[0]}; use /skills")
        return CommandResult(False)
    routed = app.command_router.handle(text)
    if not routed.handled or routed.exit_requested:
        return routed
    command, _, argument = text.partition(" ")
    argument = argument.strip()
    if command == "/clear":
        app.last_result = None
        app.conversation = ()
        # Detach from the old session so history cannot leak back in.
        app.session_id = None
        return routed
    if command == "/sessions":
        sessions = await app.sessions.list_sessions()
        return CommandResult(
            True,
            json.dumps(
                [
                    {
                        "session_id": session.session_id,
                        "updated_at": session.updated_at.isoformat(),
                        "current": session.session_id == app.session_id,
                    }
                    for session in sessions
                ],
                separators=(",", ":"),
            ),
        )
    if command == "/resume":
        return await _resume_status(app, argument)
    if command == "/tasks":
        return CommandResult(True, await _list_tasks(app))
    if command == "/memory":
        if not argument:
            return CommandResult(True, "usage: /memory <query>")
        records = await app.memory.search(argument)
        return CommandResult(
            True,
            json.dumps(
                [
                    {
                        "memory_id": record.memory_id,
                        "text": record.text,
                        "source_ref": record.source_ref,
                    }
                    for record in records
                ],
                separators=(",", ":"),
            ),
        )
    if command == "/skills":
        if argument == 'errors':
            return CommandResult(True, json.dumps(app.skill_diagnostics))
        return CommandResult(
            True,
            json.dumps(
                [
                    {
                        "name": skill.name,
                        "scope": skill.scope,
                        "source_id": skill.source_id,
                    }
                    for skill in app.skills
                ],
                separators=(",", ":"),
            ),
        )
    if command == "/agents":
        return CommandResult(
            True,
            json.dumps(
                {
                    "spawned": app.subagents.spawned_count,
                    "running": app.subagents.running_count,
                },
                separators=(",", ":"),
            ),
        )
    if command == "/jobs":
        tasks = await app.scheduler.list()
        return CommandResult(
            True,
            json.dumps(
                {
                    "worker": (
                        "running"
                        if app.scheduler_worker is not None
                        else "stopped"
                    ),
                    "unbound_legacy_tasks": await app.scheduler.unbound_count(),
                    "tasks": [
                        {
                            "task_id": task.task_id,
                            "status": task.status.value,
                            "run_at": task.run_at.isoformat(),
                            "child_run_id": task.child_run_id,
                        }
                        for task in tasks
                    ],
                },
                separators=(",", ":"),
            ),
        )
    if command == "/cancel":
        if not argument:
            return CommandResult(True, "usage: /cancel <job-id>")
        return CommandResult(
            True,
            json.dumps(
                {"cancelled": await (app.scheduler_runtime.cancel(argument)
                                     if app.scheduler_runtime else app.scheduler.cancel(argument))},
                separators=(",", ":"),
            ),
        )
    if command == "/trace":
        return _trace_command(app, argument)
    if command == "/metrics":
        metrics = (
            metrics_from_events(app.last_result.events)
            if app.last_result is not None
            else metrics_from_events(())
        )
        return CommandResult(
            True,
            json.dumps(asdict(metrics), separators=(",", ":")),
        )
    if command == "/sandbox":
        return CommandResult(True, await app._sandbox_status())
    if command == "/changes":
        return CommandResult(True, _changes_report(app))
    if command == "/apply":
        return await _apply_command(app, argument)
    if command == "/mcp":
        return CommandResult(
            True,
            json.dumps(list(app.mcp_status), separators=(",", ":"))
            if app.mcp_status
            else "no MCP servers configured",
        )
    return routed



async def _resume_status(app: MiniClawApp, session_id: str) -> CommandResult:
    if not session_id:
        return CommandResult(True, "usage: /resume <session-id>")
    run = await app.sessions.latest_run_for_session(session_id)
    if run is None:
        return CommandResult(True, f"session not found: {session_id}")
    try:
        checkpoint = await app.checkpoints.latest_for_run(run.run_id)
    except PersistenceError as error:
        return _resume_result(
            session_id,
            run,
            None,
            RecoveryDecision.UNRECOVERABLE,
            str(error),
        )

    pending = (
        pending_tool_executions(checkpoint.messages, app.tools)
        if checkpoint is not None
        else ()
    )
    decision = decide_recovery(
        checkpoint_valid=checkpoint is not None,
        executions=pending,
    )
    reason = "restored from checkpoint"
    if decision is RecoveryDecision.UNRECOVERABLE:
        reason = "no usable checkpoint for this session"
    elif decision is RecoveryDecision.REQUIRES_APPROVAL:
        # Replaying a non-idempotent tool could duplicate a side effect,
        # so a human decides whether this history may be resumed.
        approved = await _approve_resume(app, run, pending)
        if not approved:
            return _resume_result(
                session_id,
                run,
                checkpoint,
                decision,
                "resume denied: unfinished non-idempotent tool calls",
            )
        reason = "approved despite unfinished non-idempotent tool calls"

    if decision is RecoveryDecision.UNRECOVERABLE:
        return _resume_result(
            session_id, run, checkpoint, decision, reason
        )
    assert checkpoint is not None
    try:
        restored = close_interrupted_tool_calls(checkpoint.messages)
    except ValueError as error:
        return _resume_result(session_id, run, checkpoint,
                                   RecoveryDecision.UNRECOVERABLE, str(error))
    app.session_id = session_id
    app.conversation = restored
    app.last_result = None
    return _resume_result(
        session_id,
        run,
        checkpoint,
        decision,
        reason,
        resumed=True,
    )



async def _approve_resume(app: MiniClawApp, run: RunRecord, pending: tuple[ToolExecutionRecord, ...]) -> bool:
    if app.agent.approval_provider is None:
        return False
    request_id = str(uuid4())
    resolution = await app.agent.approval_provider.resolve(
        PermissionRequest(
            request_id,
            run.run_id,
            pending[0].tool_call_id,
            "resume_session",
            arguments_preview({"run_id": run.run_id,
                               "pending_calls": [p.tool_call_id for p in pending],
                               "action": "restore history without replay"}),
            ToolCapabilities(risk_level=RiskLevel.HIGH, idempotent=False),
            f"{len(pending)} unfinished non-idempotent tool call(s)",
        )
    )
    return resolution.request_id == request_id and resolution.approved



def _resume_result(
    session_id: str,
    run: RunRecord,
    checkpoint: Checkpoint | None,
    decision: RecoveryDecision,
    reason: str,
    *,
    resumed: bool = False,
) -> CommandResult:
    return CommandResult(
        True,
        json.dumps(
            {
                "session_id": session_id,
                "run_id": run.run_id,
                "status": run.status,
                "checkpoint_id": (
                    checkpoint.checkpoint_id if checkpoint else None
                ),
                "decision": decision.value,
                "resumed": resumed,
                "restored_messages": (
                    len(checkpoint.messages) if resumed and checkpoint else 0
                ),
                "reason": reason,
            },
            separators=(",", ":"),
        ),
    )



async def _list_tasks(app: MiniClawApp) -> str:
    tasks = await app.plans.list_tasks()
    return json.dumps(
        [
            {
                "plan_id": plan_id,
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status.value,
            }
            for plan_id, task in tasks
        ],
        separators=(",", ":"),
    )



def _trace_command(app: MiniClawApp, argument: str) -> CommandResult:
    if argument == "show":
        files = tuple(sorted(app.paths.traces.glob("*.jsonl")))
        return CommandResult(
            True,
            "\n".join(str(path) for path in files) or "no traces",
        )
    if argument in {"on", "off"}:
        return CommandResult(
            True,
            "trace collection is always on in this version",
        )
    return CommandResult(True, "usage: /trace on|off|show")



def _changes_report(app: MiniClawApp) -> str:
    changes = app.workspace_changes()
    if not changes:
        return "no changes in the sandbox workspace"
    source = app.workspace_session.baseline
    lines = [f"sandbox: {app.workspace_session.workspace}", ""]
    for path in changes:
        state = ("deleted" if not (app.workspace_session.workspace / path).exists()
                 else "modified" if path in source else "new")
        lines.append(f"  {state:>8}  {path.as_posix()}")
    lines.append("")
    lines.append("apply with: /apply <path> [...]  or  /apply --all")
    return "\n".join(lines)



async def _apply_command(app: MiniClawApp, argument: str) -> CommandResult:
    changes = app.workspace_changes()
    if not argument:
        return CommandResult(
            True,
            "usage: /apply <path> [...] | /apply --all\n"
            "run /changes first to see what the agent wrote",
        )
    if argument == "--all":
        selected = changes
    else:
        requested = tuple(Path(part) for part in argument.split())
        unknown = [
            path.as_posix() for path in requested if path not in set(changes)
        ]
        if unknown:
            return CommandResult(
                True, f"not changed in the sandbox: {', '.join(unknown)}"
            )
        selected = requested
    if not selected:
        return CommandResult(True, "no changes in the sandbox workspace")
    try:
        applied = await app.apply_artifacts(app.config.workspace, selected)
    except PermissionError as error:
        return CommandResult(True, str(error))
    except (WorkspaceEscape, OSError) as error:
        return CommandResult(True, f"apply failed: {error}")
    return CommandResult(
        True,
        "applied to "
        f"{app.config.workspace}:\n"
        + "\n".join(f"  {path.as_posix()}" for path in applied),
    )

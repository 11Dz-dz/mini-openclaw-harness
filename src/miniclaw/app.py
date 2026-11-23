import contextlib
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from miniclaw.agent.limits import RunLimits
from miniclaw.agent.loop import AgentLoop
from miniclaw.application import SCHEDULER_POLL_SECONDS as SCHEDULER_POLL_SECONDS
from miniclaw.application import MiniClawApp
from miniclaw.cli.commands import CommandRouter
from miniclaw.config import (
    EnvironmentSecretProvider,
    MiniClawConfig,
    SecretProvider,
    validate_config_values,
)
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget
from miniclaw.core.errors import ApplicationConfigurationError
from miniclaw.mcp.bridge import register_mcp_tools
from miniclaw.mcp.client import StdioMcpClient
from miniclaw.mcp.config import McpConfigError, load_mcp_servers
from miniclaw.mcp.isolated import isolated_mcp_client
from miniclaw.mcp.types import McpError
from miniclaw.memory.context import MemoryContextProvider
from miniclaw.memory.store import SqliteMemoryStore
from miniclaw.model.base import ModelProvider
from miniclaw.model.openai_compat import OpenAICompatibleClient
from miniclaw.observability.trace import JsonlTraceSink
from miniclaw.paths import MiniClawPaths
from miniclaw.permissions.approval import (
    CliApprovalProvider,
    UnattendedApprovalProvider,
)
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.preview import SECRET_KEY, arguments_preview
from miniclaw.permissions.types import ApprovalResolution, PermissionRequest
from miniclaw.planning.store import PlanStore
from miniclaw.sandbox.docker import DockerSandbox
from miniclaw.sandbox.process import ProcessSandbox
from miniclaw.sandbox.router import SandboxCapabilityUnavailable, SandboxRouter
from miniclaw.sandbox.trusted import (
    TrustedExecutionContext,
    TrustedProcessExecutor,
)
from miniclaw.scheduler.agent_runner import AgentTaskRunner
from miniclaw.scheduler.clock import SystemClock
from miniclaw.scheduler.runtime import SchedulerRuntime
from miniclaw.scheduler.store import SchedulerStore
from miniclaw.sessions.checkpoints import FileCheckpointStore
from miniclaw.sessions.database import SessionRepository
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.skills.discovery import discover_skills
from miniclaw.skills.provider import SkillContextProvider
from miniclaw.subagents.factory import AgentLoopFactory
from miniclaw.subagents.runtime import SubagentRuntime
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.builtin.memory import make_memory_tools
from miniclaw.tools.builtin.planning import make_builtin_planning_tools
from miniclaw.tools.builtin.scheduler import make_builtin_scheduler_tools
from miniclaw.tools.builtin.shell import make_run_command_tool
from miniclaw.tools.builtin.subagents import make_builtin_subagent_tools
from miniclaw.tools.builtin.toolsets import (
    make_local_inspection_tools,
    make_web_tools,
)
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RiskLevel, ToolCapabilities
from miniclaw.web.config import SEARCH_API_KEY_VARIABLE, load_search_endpoint
from miniclaw.workspace.session import WorkspaceSession


class ApprovalProvider(Protocol):
    async def resolve(
        self,
        request: PermissionRequest,
    ) -> ApprovalResolution: ...


SCHEDULER_CONCURRENCY = 2

DEFAULT_SUBAGENT_TOOLS = (
    "read_file",
    "search_memory",
    "list_tasks",
    "git_status",
    "git_log",
    "git_diff",
    "git_show",
)

# Nothing is pre-authorized for background runs by default: granting a tool
# here lets a scheduled task perform it with no human in the loop.
DEFAULT_UNATTENDED_APPROVED_TOOLS: tuple[str, ...] = ()


async def _start_mcp_servers(
    tools: ToolRegistry,
    workspace: Path,
    *,
    approval_provider: ApprovalProvider | None = None,
    execution_workspace: Path | None = None,
    sandbox: DockerSandbox | None = None,
    isolation_unavailable: str | None = None,
) -> tuple[tuple[StdioMcpClient, ...], tuple[dict[str, object], ...]]:
    """Start configured MCP servers, degrading instead of failing startup.

    A third-party server that is missing, slow or broken must not stop the
    agent from running; it is reported as unavailable and its tools are absent.
    """
    try:
        configured = load_mcp_servers(workspace)
    except McpConfigError as error:
        return (), ({"server": "<config>", "status": "invalid", "detail": str(error)},)

    clients: list[StdioMcpClient] = []
    status: list[dict[str, object]] = []
    for server in configured:
        if not server.enabled:
            status.append({"server": server.name, "status": "disabled", "tools": []})
            continue
        if isolation_unavailable is not None:
            status.append({"server": server.name, "status": "unavailable",
                           "detail": isolation_unavailable, "tools": []})
            continue
        # Take a private snapshot before asking; approval never trusts a path
        # or a mutable configuration that can change while the prompt is open.
        launch = asdict(server)
        fingerprint = hashlib.sha256(
            json.dumps(launch, sort_keys=True).encode()
        ).hexdigest()
        request_id = str(uuid4())
        preview = {**launch, "env": {key: "<redacted>" for key in server.env}}
        preview["sha256"] = fingerprint
        preview["workspace"] = "/workspace (isolated copy)"
        preview["network"] = "deny"
        approved = False
        if approval_provider is not None:
            try:
                resolution = await approval_provider.resolve(PermissionRequest(
                    request_id, "startup", f"mcp:{server.name}", "start_mcp_server",
                    arguments_preview(preview),
                    ToolCapabilities(subprocess=True, risk_level=RiskLevel.HIGH,
                                     idempotent=False),
                    "Trust this external MCP program for this startup only",
                ))
                approved = resolution.request_id == request_id and resolution.approved
            except (EOFError, OSError):
                approved = False
        if not approved:
            status.append({"server": server.name, "status": "untrusted", "tools": []})
            continue
        client = None
        try:
            if execution_workspace is None:
                raise McpError("MCP requires a workspace copy")
            client = await isolated_mcp_client(launch, execution_workspace, sandbox)
            info = await client.start()
            names = await register_mcp_tools(tools, client, server.name)
        except (McpError, OSError) as error:
            if client is not None:
                await client.aclose()
            status.append(
                {
                    "server": server.name,
                    "status": "unavailable",
                    "detail": str(error),
                    "tools": [],
                }
            )
            continue
        clients.append(client)
        status.append(
            {
                "server": server.name,
                "status": "ready",
                "version": info.version,
                "tools": list(names),
            }
        )
    return tuple(clients), tuple(status)


async def create_app(
    config: MiniClawConfig,
    *,
    provider: ModelProvider | None = None,
    secret_provider: SecretProvider | None = None,
    approval_provider: ApprovalProvider | None = None,
    unattended_approved_tools: tuple[str, ...] = DEFAULT_UNATTENDED_APPROVED_TOOLS,
    docker_workspace_aliases: tuple[str, ...] = (),
    coding_tools: bool = False,
    strip_previous_turn_reasoning: bool = False,
) -> MiniClawApp:
    validate_config_values({"execution_mode": config.execution_mode, "trusted_user": config.trusted_user,
                            "require_strong_sandbox": config.require_strong_sandbox})
    workspace_root = config.workspace.resolve()
    data_root = config.data_dir.resolve()
    if data_root.is_relative_to(workspace_root) or workspace_root.is_relative_to(data_root):
        raise ApplicationConfigurationError("data_dir and workspace must not overlap")
    trace_secrets: tuple[str, ...] = ()
    api_key: str | None = None
    internal_provider: ModelProvider | None = None
    if provider is None:
        secrets = secret_provider or EnvironmentSecretProvider()
        api_key = secrets.get("OPENAI_API_KEY")
        if not config.base_url:
            raise ApplicationConfigurationError("base_url is required for a real model")
        if api_key is None or not api_key.strip():
            raise ApplicationConfigurationError(
                "API key is required for a real model; "
                "run: miniclaw config set api_key or set OPENAI_API_KEY"
            )

    process_sandbox = ProcessSandbox()
    docker_sandbox = DockerSandbox(
        image=config.docker_image,
        allowed_images=(config.docker_image,),
        workspace_aliases=docker_workspace_aliases,
    )
    trusted_executor = TrustedProcessExecutor(config.trusted_user) if config.execution_mode == "trusted" else None
    sandbox = SandboxRouter(process=process_sandbox, strong=docker_sandbox, trusted=trusted_executor)
    try:
        selected_sandbox = await sandbox.select(
            ToolCapabilities(subprocess=True),
            require_strong=config.require_strong_sandbox,
            mode=config.execution_mode,
        )
    except SandboxCapabilityUnavailable as error:
        if config.require_strong_sandbox or config.execution_mode == "trusted":
            raise ApplicationConfigurationError(str(error)) from error
        # Keep file tools usable without silently running unisolated commands.
        selected_sandbox = None

    if provider is None:
        assert config.base_url is not None
        assert api_key is not None
        try:
            provider = OpenAICompatibleClient(config.base_url, api_key)
        except httpx.InvalidURL as error:
            raise ApplicationConfigurationError(
                "base_url must be a valid HTTP(S) URL"
            ) from error
        internal_provider = provider
        trace_secrets = (api_key,)

    reasoning_circuit_breaker = (
        config.model in {"deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"}
        and bool(config.base_url)
        and urlsplit(config.base_url).hostname == "dashscope.aliyuncs.com"
    )

    trace_secrets += tuple(value for key, value in os.environ.items()
                           if SECRET_KEY.search(key) and len(value) >= 4)

    try:
        paths = MiniClawPaths.create(config.data_dir)
        config.workspace.mkdir(parents=True, exist_ok=True)
        workspace_session = (
            trusted_executor.create_session(paths.sandboxes, config.workspace)
            if trusted_executor else WorkspaceSession.create(paths.sandboxes, config.workspace)
        )
        sessions = SessionRepository(paths.state_database)
        await sessions.initialize()
        checkpoints = FileCheckpointStore(paths.checkpoints, sessions, secrets=trace_secrets)
        memory = SqliteMemoryStore(paths.state_database, paths.memory_sources, secret_values=trace_secrets)
        await memory.initialize()
        plans = PlanStore(paths.plans_database)
        await plans.initialize()
        scheduler = SchedulerStore(paths.scheduler_database, workspace=config.workspace)
        await scheduler.initialize()

        trace_sink = JsonlTraceSink(paths.traces, secrets=trace_secrets)
        event_store = JsonlEventStore(paths.data / "events.jsonl", secrets=trace_secrets)
        context_builder = ContextBuilder(CharacterTokenEstimator())
        context_budget = ContextBudget(
            config.context_tokens,
            config.reserve_output_tokens,
        )
        permission_policy = DefaultPermissionPolicy(trusted_state_tools=(
            "remember", "create_plan", "start_task", "complete_task", "verify_task", "fail_task",
            "list_tasks", "list_todos", "cancel_scheduled_task",
        ))
        resolved_approval = approval_provider or CliApprovalProvider()
        skill_diagnostics = []
        skills = discover_skills(config.workspace, diagnostics=skill_diagnostics)
        context_providers = (
            SkillContextProvider(skills),
            MemoryContextProvider(memory),
        )
        if trusted_executor:
            context_providers += (TrustedExecutionContext(),)

        tools = ToolRegistry(execution_recorder=plans.record_execution)
        mounts = docker_sandbox.workspace_mounts if selected_sandbox is docker_sandbox else ()
        identity = ((trusted_executor.uid, trusted_executor.gid)
                    if trusted_executor and os.geteuid() == 0 else None)
        for tool in make_file_tools(execution_identity=identity, mounts=mounts):
            tools.register(tool)
        command_backend = selected_sandbox or process_sandbox
        tools.register(make_run_command_tool(command_backend, trusted=trusted_executor is not None))
        for tool in make_memory_tools(memory):
            tools.register(tool)
        for tool in make_builtin_planning_tools(plans):
            tools.register(tool)
        child_factory = AgentLoopFactory(
            provider,
            config.model,
            workspace=workspace_session.workspace,
            context_builder=context_builder,
            context_budget=context_budget,
            permission_policy=permission_policy,
            approval_provider=resolved_approval,
            session_repository=sessions,
            checkpoint_store=checkpoints,
            event_store=event_store,
            trace_sinks=(trace_sink,),
            max_depth=2,
        )
        subagents = SubagentRuntime(
            child_factory,
            max_depth=2,
            max_children=4,
            parent_allowed_tools=DEFAULT_SUBAGENT_TOOLS,
        )
        child_factory.runtime = subagents
        for tool in make_builtin_subagent_tools(subagents, DEFAULT_SUBAGENT_TOOLS):
            tools.register(tool)
        for tool in make_builtin_scheduler_tools(scheduler):
            tools.register(tool)
        for tool in make_local_inspection_tools(
            command_backend, trusted=trusted_executor is not None, mounts=mounts,
        ):
            tools.register(tool)
        if selected_sandbox is None:
            for tool in tools.values():
                if tool.spec.capabilities.subprocess:
                    tools.disable(
                        tool.spec.name,
                        sandbox.unavailable_reason or "isolated execution is unavailable",
                    )
        if coding_tools:
            for tool in tools.values():
                if tool.spec.name not in {
                    "read_file", "search_text", "write_file", "replace_text", "run_command", "git_status", "git_diff", "git_log", "git_show",
                }:
                    tools.disable(tool.spec.name, "outside the coding tool profile")
            web_closeables, mcp_clients, mcp_status = (), (), ()
        else:
            search_endpoint = load_search_endpoint(config.workspace)
            web_tools, web_closeables = make_web_tools(
                search_endpoint=search_endpoint,
                # Only ask for a credential that has somewhere to be used.
                search_api_key=(
                    (secret_provider or EnvironmentSecretProvider()).get(SEARCH_API_KEY_VARIABLE)
                    if search_endpoint is not None else None
                ),
            )
            for tool in web_tools:
                tools.register(tool)
            mcp_clients, mcp_status = await _start_mcp_servers(
                tools, config.workspace, approval_provider=resolved_approval,
                execution_workspace=workspace_session.workspace,
                sandbox=docker_sandbox,
                isolation_unavailable=("MCP requires an isolated backend; trusted host execution only enables Shell/Git"
                                       if trusted_executor else sandbox.unavailable_reason
                                       if selected_sandbox is None else None),
            )
        child_factory.tools = tools

        def make_agent(profile_tools: ToolRegistry, approval: ApprovalProvider) -> AgentLoop:
            return AgentLoop(
                provider,
                config.model,
                limits=RunLimits(config.max_turns, config.max_tool_calls),
                tools=profile_tools,
                workspace=workspace_session.workspace,
                session_repository=sessions,
                checkpoint_store=checkpoints,
                event_store=event_store,
                context_builder=context_builder,
                context_budget=context_budget,
                permission_policy=permission_policy,
                approval_provider=approval,
                trace_sinks=(trace_sink,),
                context_providers=context_providers,
                strip_previous_turn_reasoning=strip_previous_turn_reasoning,
                reasoning_circuit_breaker=reasoning_circuit_breaker,
                coding_guidance=coding_tools,
            )

        agent = make_agent(tools, resolved_approval)
        # Scheduled work runs without a human, so it gets its own loop whose
        # approval provider denies anything the policy wants to ask about.
        # It also loses delegate_task: children are built by a factory holding
        # the interactive approval provider, which would otherwise let a
        # background run reach a human prompt through its child.
        unattended_tools = tools.subset(
            tuple(
                tool.spec.name
                for tool in tools.values()
                if tool.spec.name != "delegate_task"
            )
        )
        scheduler_agent = make_agent(
            unattended_tools, UnattendedApprovalProvider(unattended_approved_tools),
        )
        scheduler_runtime = SchedulerRuntime(
            scheduler,
            AgentTaskRunner(scheduler_agent),
            SystemClock(),
            max_concurrency=SCHEDULER_CONCURRENCY,
        )
        return MiniClawApp(
            config,
            paths,
            agent,
            tools,
            sessions,
            checkpoints,
            memory,
            plans,
            scheduler,
            subagents,
            skills,
            trace_sink,
            workspace_session,
            provider,
            sandbox,
            CommandRouter(),
            scheduler_runtime,
            mcp_clients,
            mcp_status,
            web_closeables,
            skill_diagnostics=tuple(skill_diagnostics),
        )
    except BaseException:
        if internal_provider is not None:
            close = getattr(internal_provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(BaseException):
                    await close()
        raise

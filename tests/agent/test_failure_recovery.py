import pytest

from miniclaw.agent.limits import RunLimits
from miniclaw.agent.loop import AgentLoop, CompletionChecked, ContextInjected, RunFailed
from miniclaw.agent.state import RunStatus
from miniclaw.app import create_app
from miniclaw.cli.repl import AppRepl
from miniclaw.config import MiniClawConfig
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget
from miniclaw.core.errors import AuthenticationError
from miniclaw.core.messages import (
    Message,
    ReasoningDelta,
    ResponseCompleted,
    TextContent,
    TextDelta,
    ToolCallDelta,
)
from miniclaw.model.encoding import encode_request
from miniclaw.model.scripted import ScriptedModel
from miniclaw.observability.metrics import metrics_from_events
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.sandbox.capabilities import SandboxCapabilities, SandboxStrength
from miniclaw.sandbox.types import SandboxResult
from miniclaw.sessions.database import SessionRepository
from miniclaw.tools.registry import ToolRegistry


async def test_output_truncation_discards_the_entire_batch_and_recovers(tmp_path):
    model = ScriptedModel((
        (ToolCallDelta(0, 'earlier', 'write_file', '{"path":"earlier","content":"keep"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('discard this'),
         ToolCallDelta(0, 'ready', 'write_file', '{"path":"unreviewed","content":"no"}'),
         ToolCallDelta(1, 'partial', 'write_file', '{"path":'),
         ResponseCompleted('length')),
        (ToolCallDelta(0, 'corrected', 'write_file', '{"path":"corrected","content":"yes"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('done'), ResponseCompleted('stop')),
    ))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'), provider=model)
    app.agent.permission_policy = None
    app.agent.limits = RunLimits(max_turns=4, max_tool_calls=2)
    try:
        result = await app.run_prompt('write files')
        workspace = app.workspace_session.workspace
        assert result.status is RunStatus.COMPLETED
        assert not (workspace / 'unreviewed').exists()
        assert (workspace / 'earlier').read_text() == 'keep'
        assert (workspace / 'corrected').read_text() == 'yes'
        assert any(event.payload == ContextInjected(('model:output-truncation',)) for event in result.events)
        assert [message.role for message in model.requests[2].messages] == ['user', 'assistant', 'tool', 'user']
        feedback = model.requests[2].messages[-1].content[0].text
        assert 'Interrupted tools: write_file' in feedback
        assert metrics_from_events(result.events).truncated_responses == 1
        assert 'discard this' not in repr(model.requests[2].messages)
        checkpoint = await app.checkpoints.latest_for_run(result.run_id)
        assert checkpoint.messages == result.messages
        assert checkpoint.completed_tool_executions == ('earlier', 'corrected')
    finally:
        await app.aclose()


@pytest.mark.parametrize(('limit', 'turns', 'calls'), [(0, 5, 1), (1, 1, 1), (1, 5, 2)])
async def test_output_truncation_retry_respects_retry_and_turn_limits(limit, turns, calls):
    cut = (TextDelta('partial text'), ResponseCompleted('length'))
    model = ScriptedModel((cut,) * calls)
    result = await AgentLoop(
        model, 'fake', limits=RunLimits(max_turns=turns, max_output_truncation_retries=limit),
    ).run('go')
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == calls
    assert 'output token limit' in result.output
    assert [message.role for message in result.messages] == ['user']


async def test_reasoning_recovery_suspends_thinking_until_a_tool_succeeds(tmp_path):
    spin = (ReasoningDelta('private reasoning'), ResponseCompleted('length'))
    model = ScriptedModel((spin, spin, (ResponseCompleted('stop'),),
        (ToolCallDelta(0, 'first', 'write_file', '{"path":"../escape","content":"a"}'),
         ResponseCompleted('tool_calls')),
        (ToolCallDelta(0, 'write', 'write_file', '{"path":"result.txt","content":"ok"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('done'), ResponseCompleted('stop')),
    ))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project',
                           model='deepseek-v4.1-flash', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1'),
                           provider=model, coding_tools=True)
    app.agent.permission_policy = None
    try:
        result = await app.run_prompt('produce result', required_artifacts=('result.txt',))
        assert result.status is RunStatus.COMPLETED
        assert [request.enable_thinking for request in model.requests] == [None, None, False, False, False, None]
        assert [request.tool_choice for request in model.requests] == [None, None, 'required', 'required', 'required', None]
        forced = encode_request(model.requests[2], stream=True)
        assert forced['enable_thinking'] is False and forced['tool_choice'] == 'required'
        assert 'enable_thinking' not in encode_request(model.requests[-1], stream=True)
        assert {spec['function']['name'] for spec in forced['tools']} <= {'write_file', 'run_command'}
        metrics = metrics_from_events(result.events)
        assert (metrics.reasoning_recovery_requests, metrics.reasoning_recoveries) == (3, 1)
        assert (app.workspace_session.workspace / 'result.txt').read_text() == 'ok'
    finally:
        await app.aclose()


async def test_truncated_tool_fragments_break_the_reasoning_spin_streak(tmp_path):
    spin = (ReasoningDelta('private reasoning'), ResponseCompleted('length'))
    cut = (ReasoningDelta('long reasoning'), ToolCallDelta(0, 'partial', 'write_file', '{'), ResponseCompleted('length'))
    model = ScriptedModel((spin, cut, spin, (TextDelta('done'), ResponseCompleted('stop'))))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'), provider=model)
    app.agent.reasoning_circuit_breaker = True
    try:
        result = await app.run_prompt('check interrupted tool streaming')
        assert result.status is RunStatus.COMPLETED
        assert all(request.enable_thinking is None for request in model.requests)
    finally:
        await app.aclose()


async def test_missing_artifact_continues_with_the_missing_path(tmp_path):
    model = ScriptedModel((
        (TextDelta('premature completion'), ResponseCompleted('stop')),
        (ToolCallDelta(0, 'result', 'write_file', '{"path":"result.json","content":"[]"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('done'), ResponseCompleted('stop')),
    ))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'), provider=model)
    app.agent.permission_policy = None
    workspace = app.workspace_session.workspace
    (workspace / 'existing.txt').write_text('already generated')
    try:
        result = await app.run_prompt('write files', required_artifacts=('existing.txt', 'result.json'), force_artifact_tool=True)
        assert result.status is RunStatus.COMPLETED
        feedback = model.requests[1].messages[-1].content[0].text
        assert model.requests[1].tool_choice == 'required'
        assert model.requests[0].tool_choice is None and model.requests[2].tool_choice is None
        assert 'result.json' in feedback and 'existing.txt' not in feedback
        assert 'premature completion' not in repr(result.messages)
        assert (workspace / 'result.json').read_text() == '[]'
        assert metrics_from_events(result.events).first_artifact_turn == 2
    finally:
        await app.aclose()


async def test_missing_artifacts_cannot_be_satisfied_outside_workspace_or_by_a_directory(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'directory').mkdir()
    (tmp_path / 'outside.txt').write_text('outside')
    model = ScriptedModel(((ResponseCompleted('stop'),),) * 3)
    result = await AgentLoop(model, 'fake', workspace=workspace).run(
        'go', required_artifacts=('directory', '../outside.txt'),
    )
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 3
    assert 'directory' in result.output and '../outside.txt' in result.output
    assert [message.role for message in result.messages] == ['user']


async def test_cross_turn_reasoning_removal_keeps_current_tool_protocol(tmp_path):
    history = (Message('user', (TextContent('prior question'),)),
               Message('assistant', (TextContent('prior answer'),), 'prior reasoning'))
    model = ScriptedModel((
        (ReasoningDelta('current reasoning'), ToolCallDelta(0, 'read', 'read_file', '{"path":"file"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('done'), ResponseCompleted('stop')),
    ))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'), provider=model,
                           coding_tools=True, strip_previous_turn_reasoning=True)
    try:
        (app.workspace_session.workspace / 'file').write_text('file contents')
        result = await app.agent.run('current question', history=history)
        assert result.status is RunStatus.COMPLETED
        assert model.requests[0].messages[1].reasoning_content is None
        assert model.requests[1].messages[3].reasoning_content == 'current reasoning'
        assert result.messages[1].reasoning_content == 'prior reasoning'
        assert 'remember' not in {spec['function']['name'] for spec in model.requests[0].tools}
    finally:
        await app.aclose()


async def test_docker_verification_runs_after_artifacts_exist_and_can_request_a_fix(tmp_path, monkeypatch):
    async def docker_available(self):
        return SandboxCapabilities(True, SandboxStrength.STRONG, True, True, True, 'available')

    checks = []

    async def verify(self, command, policy):
        checks.append(command)
        assert self.workspace_mounts == ('/workspace', '/app')
        assert policy.filesystem == 'read' and policy.network == 'deny'
        assert command.argv == ('python', '-c', 'trusted check')
        assert command.cwd == policy.workspace
        valid = (command.cwd / 'result.json').read_text() == '[]'
        return SandboxResult(0 if valid else 1, 'checker started', 'expected a JSON list', ())

    monkeypatch.setattr('miniclaw.sandbox.docker.DockerSandbox.capabilities', docker_available)
    monkeypatch.setattr('miniclaw.sandbox.docker.DockerSandbox.execute', verify)
    model = ScriptedModel((
        (ResponseCompleted('stop'),),
        (ToolCallDelta(0, 'write', 'write_file', '{"path":"/app/result.json","content":"{}"}'),
         ResponseCompleted('tool_calls')),
        (ResponseCompleted('stop'),),
        (ToolCallDelta(0, 'edit', 'replace_text',
                       '{"path":"/workspace/result.json","old_text":"{}","new_text":"[]"}'),
         ResponseCompleted('tool_calls')),
        (ResponseCompleted('stop'),),
    ))
    app = await create_app(
        MiniClawConfig(tmp_path / 'data', tmp_path / 'project', require_strong_sandbox=True),
        provider=model,
        docker_workspace_aliases=('/app',),
    )
    app.agent.permission_policy = None
    try:
        result = await app.run_prompt(
            'produce a list', required_artifacts=('result.json',),
            verification_command=('python', '-c', 'trusted check'),
        )
        assert result.status is RunStatus.COMPLETED, (result.output, result.messages)
        assert len(checks) == 2
        assert [event.payload.passed for event in result.events
                if isinstance(event.payload, CompletionChecked)] == [False, True]
        assert 'expected a JSON list' in model.requests[3].messages[-1].content[0].text
        assert 'checker started' in model.requests[3].messages[-1].content[0].text
        assert 'Check 1 failed' in model.requests[3].messages[-1].content[0].text
        assert not (app.config.workspace / 'result.json').exists()
    finally:
        await app.aclose()


async def test_run_local_active_verification_detects_identical_failure(tmp_path):
    model = ScriptedModel((
        (ToolCallDelta(0, 'check-1', 'verify_workspace', '{}'), ResponseCompleted('tool_calls')),
        (ToolCallDelta(0, 'check-2', 'verify_workspace', '{}'), ResponseCompleted('tool_calls')),
        (TextDelta('fixed'), ResponseCompleted('stop')),
    ))
    checks = iter(('cost too high', 'cost too high', None))

    async def verify(path):
        assert path == tmp_path
        return next(checks)

    registry = ToolRegistry()
    result = await AgentLoop(model, 'fake', workspace=tmp_path, tools=registry,
                             permission_policy=DefaultPermissionPolicy()).run(
        'produce a plan', completion_check=verify, active_verification=True, pivot_on_stall=True,
    )
    assert result.status is RunStatus.COMPLETED
    assert 'Choose a different approach' in result.messages[-2].content[0].output
    assert not registry.specs()
    metrics = metrics_from_events(result.events)
    assert (metrics.workspace_verifications, metrics.verification_stalls) == (2, 1)


@pytest.mark.parametrize("malformed", [
    (ToolCallDelta(1, 'invalid', 'read_file', '{"path":'),),
    (ToolCallDelta(0, 'conflicting', 'read_file', '{}'),),
])
async def test_invalid_batch_never_executes_and_model_can_correct_it(tmp_path, malformed):
    model = ScriptedModel((
        (ToolCallDelta(0, 'good', 'write_file', '{"path":"unreviewed","content":"no"}'),
         *malformed, ResponseCompleted('tool_calls')),
        (ToolCallDelta(0, 'corrected', 'write_file', '{"path":"corrected","content":"yes"}'),
         ResponseCompleted('tool_calls')),
        (TextDelta('done'), ResponseCompleted('stop')),
    ))
    app = await create_app(
        MiniClawConfig(tmp_path / 'data', tmp_path / 'project'),
        provider=model,
    )
    # Isolate the assembly behaviour from the interactive approval policy.
    app.agent.permission_policy = None
    try:
        result = await app.run_prompt('write a file')
        workspace = app.workspace_session.workspace
        assert result.status is RunStatus.COMPLETED
        assert not (workspace / 'unreviewed').exists()
        assert (workspace / 'corrected').read_text() == 'yes'
        assert [message.role for message in model.requests[1].messages] == ['user', 'user']
        assert 'None of its calls ran.' in model.requests[1].messages[-1].content[0].text
        assert not any(message.role == 'system' for message in result.messages)
        assert [message.role for message in result.messages] == ['user', 'assistant', 'tool', 'assistant']
    finally:
        await app.aclose()


@pytest.mark.parametrize(('turn_limit', 'requests'), [(1, 1), (2, 2), (9, 3)])
async def test_invalid_batch_corrections_respect_both_limits(turn_limit, requests):
    invalid = (ToolCallDelta(0, 'invalid', 'read_file', '{"path":'),
               ResponseCompleted('tool_calls'))
    model = ScriptedModel((invalid,) * requests)
    result = await AgentLoop(
        model, 'fake', tools=ToolRegistry(), limits=RunLimits(max_turns=turn_limit),
        context_builder=ContextBuilder(CharacterTokenEstimator()),
        context_budget=ContextBudget(2000, 200),
    ).run('go')
    assert result.status is RunStatus.FAILED
    assert len(model.requests) == requests
    assert [message.role for message in result.messages] == ['user']


async def test_unknown_tool_becomes_a_tool_error_with_permission_policy_enabled():
    model = ScriptedModel(((ToolCallDelta(0, 'one', 'nonexistent', '{}'),
                            ResponseCompleted('tool_calls')),
                           (TextDelta('recovered'), ResponseCompleted('stop'))))
    result = await AgentLoop(model, 'fake', tools=ToolRegistry(),
        permission_policy=DefaultPermissionPolicy()).run('go')
    assert result.status is RunStatus.COMPLETED
    assert result.messages[2].content[0].is_error
    assert result.messages[2].content[0].output == 'tool not found'


async def test_model_failure_is_terminal_without_a_checkpoint_store(tmp_path):
    class Broken:
        async def stream(self, request):
            raise AuthenticationError('synthetic-secret-must-not-leak')
            yield

    repo = SessionRepository(tmp_path / 'state.db')
    await repo.initialize()
    result = await AgentLoop(Broken(), 'fake', session_repository=repo).run('go')
    assert result.status is RunStatus.FAILED
    assert (await repo.get_run(result.run_id)).status == 'failed'
    assert any(isinstance(e.payload, RunFailed) for e in result.events)
    assert 'synthetic-secret' not in result.output


async def test_repl_accepts_next_prompt_after_model_failure(tmp_path):
    class Recovering:
        count = 0

        async def stream(self, request):
            self.count += 1
            if self.count == 1:
                raise AuthenticationError('secret detail')
            yield TextDelta('next turn works')
            yield ResponseCompleted('stop')

    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'),
                           provider=Recovering())
    output = []
    repl = AppRepl(app, write=output.append)
    try:
        assert await repl.process('first') is False
        assert app.last_result.status is RunStatus.FAILED
        assert await repl.process('second') is False
        assert app.last_result.status is RunStatus.COMPLETED
    finally:
        await app.aclose()
    assert 'secret detail' not in ''.join(output)

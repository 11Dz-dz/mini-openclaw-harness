import asyncio
import tempfile
from pathlib import Path

from miniclaw.agent.loop import AgentLoop, ApprovalRequested
from miniclaw.core.messages import ResponseCompleted, TextDelta, ToolCallDelta
from miniclaw.evals.assertions import evaluate_assertion
from miniclaw.evals.loader import _ASSERTIONS, _FAULTS, EvalLoadError
from miniclaw.evals.types import EvalCase, EvalResult
from miniclaw.model.scripted import ScriptedModel
from miniclaw.observability.metrics import metrics_from_events
from miniclaw.observability.trace import JsonlTraceSink
from miniclaw.permissions.approval import UnattendedApprovalProvider
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.sessions.checkpoints import FileCheckpointStore
from miniclaw.sessions.database import SessionRepository
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import RegisteredTool
from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path


def _event(raw: dict[str, object]):
    if not isinstance(raw, dict):
        raise EvalLoadError('script event must be an object')
    event_type = raw.get("type")
    if event_type == "text":
        return TextDelta(str(raw.get("text", "")))
    if event_type == "complete":
        return ResponseCompleted(str(raw.get("reason", "stop")))
    if event_type == "tool_delta":
        return ToolCallDelta(int(raw.get("index", 0)),
            str(raw["id"]) if raw.get("id") is not None else None,
            str(raw["name"]) if raw.get("name") is not None else None,
            str(raw.get("arguments", "")))
    raise EvalLoadError("unknown script event")


class EvalRunner:
    async def run(self, case: EvalCase) -> EvalResult:
        if any(a.kind not in _ASSERTIONS for a in case.assertions):
            raise EvalLoadError('unsupported assertion kind')
        for fault in case.fault_plan:
            if fault.get('type') not in _FAULTS:
                raise EvalLoadError('unsupported fault kind')
            if fault['type'] == 'model_disconnect_after_event':
                value = fault.get('after_event', 1)
                if type(value) is not int or value < 0:
                    raise EvalLoadError('after_event must be a nonnegative integer')
            elif fault.get('tool', 'read_file') not in {'read_file', 'write_file'}:
                raise EvalLoadError('fault tool must be an offline file tool')
        turns, current = [], []
        for raw in case.script:
            event = _event(raw)
            current.append(event)
            if isinstance(event, ResponseCompleted):
                turns.append(tuple(current))
                current = []
        if current:
            turns.append(tuple(current))
        scripted = ScriptedModel(tuple(turns))
        triggered = set()

        class Model:
            emitted = 0

            async def stream(self, request):
                async for event in scripted.stream(request):
                    for index, fault in enumerate(case.fault_plan):
                        if (index not in triggered and fault['type'] == 'model_disconnect_after_event'
                                and self.emitted >= fault.get('after_event', 1)):
                            triggered.add(index)
                            raise ConnectionError('injected model disconnect')
                    self.emitted += 1
                    yield event

        with tempfile.TemporaryDirectory(prefix='miniclaw-eval-') as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            for name, text in case.files.items():
                target = resolve_workspace_path(workspace, name)
                if len(text.encode()) > 65536:
                    raise EvalLoadError('fixture file exceeds limit')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            observations = {'called_tools': [], 'sandbox_violation_count': 0}
            tools = ToolRegistry()

            def wrapped(tool):
                async def invoke(arguments, context):
                    observations['called_tools'].append(tool.spec.name)
                    for index, fault in enumerate(case.fault_plan):
                        if (index in triggered or fault.get('tool', 'read_file') != tool.spec.name
                                or fault['type'] == 'model_disconnect_after_event'):
                            continue
                        triggered.add(index)
                        if fault['type'] == 'tool_raise':
                            raise RuntimeError('injected tool failure')
                        if fault['type'] == 'tool_timeout':
                            await asyncio.wait_for(asyncio.sleep(10), .001)
                        if fault['type'] == 'sandbox_violation':
                            arguments = {**arguments, 'path': '../outside-eval-workspace'}
                    try:
                        return await tool.handler(arguments, context)
                    except WorkspaceEscape:
                        observations['sandbox_violation_count'] += 1
                        raise
                return invoke

            for tool in make_file_tools():
                tools.register(RegisteredTool(tool.spec, wrapped(tool)))
            repo = SessionRepository(root / 'state.db')
            await repo.initialize()
            checkpoints = FileCheckpointStore(root / 'checkpoints', repo)
            trace = JsonlTraceSink(root / 'traces')
            result = await AgentLoop(Model(), 'fake', tools=tools, workspace=workspace,
                permission_policy=DefaultPermissionPolicy(),
                approval_provider=UnattendedApprovalProvider(case.approved_tools),
                session_repository=repo, checkpoint_store=checkpoints,
                event_store=JsonlEventStore(root / 'events.jsonl'), trace_sinks=(trace,)).run(case.prompt)
            observations['approval_count'] = sum(isinstance(e.payload, ApprovalRequested)
                                                  for e in result.events)
            observations['checkpoint_count'] = len(tuple((root / 'checkpoints').glob('*.json')))
            observations['trace_text'] = ''.join(p.read_text() for p in (root / 'traces').glob('*.jsonl'))
            failures = [failure for assertion in case.assertions
                        if (failure := evaluate_assertion(assertion, result, observations)) is not None]
            failures.extend(f'fault was not triggered: {case.fault_plan[i]["type"]}'
                            for i in range(len(case.fault_plan)) if i not in triggered)
            return EvalResult(case.case_id, not failures, tuple(failures),
                              metrics_from_events(result.events), result.run_id)

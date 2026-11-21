import json

from miniclaw.agent.loop import AgentLoop
from miniclaw.core.messages import ResponseCompleted, ToolCall
from miniclaw.model.scripted import ScriptedModel
from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.preview import arguments_preview
from miniclaw.permissions.types import ApprovalResolution
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.registry import ToolRegistry


def test_approval_preserves_action_and_redacts_nested_credentials():
    preview = arguments_preview({
        'path': 'src/config.py', 'argv': ['tool', '--token', 'synthetic-A'],
        'nested': {'api_key': 'synthetic-B', 'destination': 'output.txt'},
        'url': 'https://user:synthetic-C@example.com/path?token=synthetic-D&x=2',
    })
    data = json.loads(preview)
    assert data['path'] == 'src/config.py'
    assert data['argv'][0] == 'tool'
    assert data['nested']['destination'] == 'output.txt'
    assert 'x=2' in data['url']
    assert 'synthetic-' not in preview


def test_different_hidden_values_still_have_different_digests():
    one = json.loads(arguments_preview({'token': 'A'}))
    two = json.loads(arguments_preview({'token': 'B'}))
    assert one['token'] == two['token'] == '<redacted>'
    assert one['_arguments_sha256'] != two['_arguments_sha256']


async def test_mutating_original_call_during_approval_cannot_change_execution(
    tmp_path, monkeypatch,
):
    original = ToolCall('one', 'write_file', {'path': 'approved', 'content': 'safe'})
    monkeypatch.setattr('miniclaw.agent.loop.ToolCallAssembler.complete',
                        lambda self: (original,))

    class Approver:
        async def resolve(self, request):
            assert 'approved' in request.arguments_preview
            original.arguments['path'] = 'not-approved'
            return ApprovalResolution(request.request_id, True, 'reviewed')

    tools = ToolRegistry()
    for tool in make_file_tools():
        tools.register(tool)
    await AgentLoop(
        ScriptedModel(((ResponseCompleted('tool_calls'),),
                       (ResponseCompleted('stop'),))), 'fake',
        tools=tools, workspace=tmp_path, permission_policy=DefaultPermissionPolicy(),
        approval_provider=Approver(),
    ).run('write')
    assert (tmp_path / 'approved').read_text() == 'safe'
    assert not (tmp_path / 'not-approved').exists()

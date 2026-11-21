from miniclaw.app import DEFAULT_SUBAGENT_TOOLS
from miniclaw.core.messages import ResponseCompleted, ToolCallDelta
from miniclaw.model.scripted import ScriptedModel
from miniclaw.subagents.factory import AgentLoopFactory
from miniclaw.subagents.types import SubagentSpec
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.registry import ToolRegistry


async def test_shared_workspace_children_cannot_write_even_if_custom_allowlist_names_write(tmp_path):
    model = ScriptedModel(((ToolCallDelta(0, 'one', 'write_file',
                            '{"path":"shared.txt","content":"overwritten"}'),
                            ResponseCompleted('tool_calls')),
                           (ResponseCompleted('stop'),)))
    tools = ToolRegistry()
    for tool in make_file_tools():
        tools.register(tool)
    factory = AgentLoopFactory(model, 'fake', workspace=tmp_path)
    factory.tools = tools
    result = await factory.run(SubagentSpec('child', 'try writing', 1, 2, 2,
                                           ('read_file', 'write_file')))
    assert not (tmp_path / 'shared.txt').exists()
    assert result.evidence == ()
    assert 'write_file' not in DEFAULT_SUBAGENT_TOOLS
    assert all(spec['function']['name'] != 'write_file' for spec in model.requests[0].tools)

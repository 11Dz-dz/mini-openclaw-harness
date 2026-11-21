import pytest

from miniclaw.core.messages import ToolCall
from miniclaw.tools.builtin.files import make_file_tools
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.types import ToolContext


@pytest.fixture
def actual_execution(tmp_path):
    async def execute(store, run_id=''):
        (tmp_path / 'evidence.txt').write_text('observed fixture result')
        tools = ToolRegistry(execution_recorder=store.record_execution)
        for tool in make_file_tools():
            tools.register(tool)
        result = await ToolExecutor(tools).execute(
            ToolCall('fixture-call', 'read_file', {'path': 'evidence.txt'}),
            ToolContext(run_id, 'fixture-session', tmp_path))
        assert not result.is_error
        assert result.execution_id
        return result.execution_id
    return execute

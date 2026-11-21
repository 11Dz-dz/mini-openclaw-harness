import pytest

from miniclaw.planning.store import PlanStore
from miniclaw.planning.tools import make_planning_tools
from miniclaw.planning.types import Plan, Task, TaskStatus
from miniclaw.tools.types import ToolContext


async def test_claim_is_distinct_from_completion_and_forged_evidence_is_rejected(
    tmp_path, actual_execution,
):
    store = PlanStore(tmp_path / 'plans.db')
    await store.initialize()
    await store.create(Plan('a', 'goal', (Task('one', 'work', (), TaskStatus.OPEN),)))
    context = ToolContext('run', 'session', tmp_path)
    tools = {t.spec.name: t for t in make_planning_tools(store)}
    await tools['start_task'].handler({'plan_id': 'a', 'task_id': 'one'}, context)
    claim = await tools['complete_task'].handler(
        {'plan_id': 'a', 'task_id': 'one', 'evidence': ['I ran all tests']}, context)
    assert claim == 'reported_done'
    assert (await store.get('a')).tasks[0].status is TaskStatus.REPORTED_DONE
    with pytest.raises(ValueError, match='successful execution'):
        await tools['verify_task'].handler(
            {'plan_id': 'a', 'task_id': 'one', 'evidence': ['execution:made-up']}, context)
    proof = await actual_execution(store, run_id='run')
    assert await tools['verify_task'].handler(
        {'plan_id': 'a', 'task_id': 'one', 'evidence': [proof]}, context) == 'done'
    assert (await store.get('a')).tasks[0].evidence == (proof,)


async def test_evidence_from_another_run_cannot_complete_the_task(tmp_path, actual_execution):
    store = PlanStore(tmp_path / 'plans.db')
    await store.initialize()
    await store.create(Plan('a', 'goal', (Task('one', 'work', (), TaskStatus.OPEN),)))
    await store.transition('a', 'one', TaskStatus.IN_PROGRESS, run_id='run-A')
    proof = await actual_execution(store, run_id='run-B')
    with pytest.raises(ValueError, match='successful execution'):
        await store.transition('a', 'one', TaskStatus.DONE, evidence=(proof,), run_id='run-A')

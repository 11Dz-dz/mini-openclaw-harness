import asyncio

import pytest

from miniclaw.planning.store import PlanStore
from miniclaw.planning.types import Plan, Task, TaskStatus


async def test_task_ids_are_local_to_their_plan(tmp_path):
    store = PlanStore(tmp_path / 'plans.db')
    await store.initialize()
    for plan_id in ('a', 'b'):
        await store.create(Plan(plan_id, 'goal', (Task('step1', 'work', (), TaskStatus.OPEN),)))
    assert len(await store.list_todos('a')) == len(await store.list_todos('b')) == 1


async def test_transition_enforces_dependencies_atomically(tmp_path, actual_execution):
    store = PlanStore(tmp_path / 'plans.db')
    await store.initialize()
    await store.create(Plan('a', 'goal', (
        Task('first', 'first', (), TaskStatus.OPEN),
        Task('second', 'second', ('first',), TaskStatus.OPEN))))
    with pytest.raises(ValueError, match='dependencies'):
        await store.transition('a', 'second', TaskStatus.IN_PROGRESS)
    await store.transition('a', 'first', TaskStatus.IN_PROGRESS)
    await store.transition('a', 'first', TaskStatus.DONE, evidence=(await actual_execution(store),))
    assert (await store.transition('a', 'second', TaskStatus.IN_PROGRESS)).attempts == 1


async def test_concurrent_starts_cannot_both_succeed(tmp_path):
    store = PlanStore(tmp_path / 'plans.db')
    await store.initialize()
    await store.create(Plan('a', 'goal', (Task('one', 'work', (), TaskStatus.OPEN),)))
    results = await asyncio.gather(
        store.transition('a', 'one', TaskStatus.IN_PROGRESS),
        store.transition('a', 'one', TaskStatus.IN_PROGRESS), return_exceptions=True)
    assert sum(not isinstance(result, BaseException) for result in results) == 1

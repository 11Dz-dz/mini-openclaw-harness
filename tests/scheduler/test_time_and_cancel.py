import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from miniclaw.scheduler.runtime import SchedulerRuntime
from miniclaw.scheduler.store import SchedulerStore
from miniclaw.scheduler.types import ScheduledTask, ScheduledTaskStatus, TaskRunResult


async def test_due_comparison_uses_instants_across_timezone_offsets(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    due = datetime(2026, 10, 26, 10, tzinfo=timezone(timedelta(hours=8)))
    await store.add(ScheduledTask.once('job', 'work', due))
    claimed = await store.claim_due(datetime(2026, 10, 26, 3, tzinfo=UTC), 1)
    assert [task.task_id for task in claimed] == ['job']


async def test_naive_schedule_is_rejected(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    with pytest.raises(ValueError, match='timezone'):
        await store.add(ScheduledTask.once('job', 'work', datetime(2026, 10, 26)))


async def test_cancel_stops_running_task_and_late_success_cannot_overwrite_it(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    now = datetime.now(UTC)
    await store.add(ScheduledTask.once('job', 'work', now))
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Clock:
        def now(self):
            return datetime.now(UTC)

    class Runner:
        async def run(self, task):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
            return TaskRunResult('child', True, None)

    runtime = SchedulerRuntime(store, Runner(), Clock(), max_concurrency=1)
    tick = asyncio.create_task(runtime.tick())
    await asyncio.wait_for(entered.wait(), 1)
    assert await runtime.cancel('job') == 1
    await asyncio.wait_for(tick, 1)
    assert stopped.is_set()
    assert not await store.finish('job', child_run_id='late', succeeded=True,
                                  error=None, owner=runtime.owner, attempt=1)
    assert (await store.get('job')).status is ScheduledTaskStatus.CANCELLED


async def test_cancel_handles_cycles_in_legacy_parent_relationships(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    now = datetime.now(UTC)
    await store.add(ScheduledTask.once('a', 'a', now, parent_task_id='b'))
    await store.add(ScheduledTask.once('b', 'b', now, parent_task_id='a'))
    assert await asyncio.wait_for(store.cancel('a'), 1) == 2

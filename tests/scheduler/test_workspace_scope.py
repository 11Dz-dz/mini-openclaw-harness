from datetime import UTC, datetime

import pytest

from miniclaw.scheduler.store import SchedulerStore
from miniclaw.scheduler.types import ScheduledTask, ScheduledTaskStatus


async def test_shared_database_cannot_run_read_cancel_or_recover_another_project(tmp_path):
    a = SchedulerStore(tmp_path / 'jobs.db', workspace=tmp_path / 'a')
    b = SchedulerStore(tmp_path / 'jobs.db', workspace=tmp_path / 'b')
    await a.initialize()
    now = datetime.now(UTC)
    await a.add(ScheduledTask.once('a-task', 'edit project A', now))
    assert await b.list() == ()
    assert await b.claim_due(now, 2) == ()
    assert await b.cancel('a-task') == 0
    with pytest.raises(KeyError):
        await b.get('a-task')
    await a.claim_due(now, 1)
    await b.recover_running(completed_child_runs=())
    assert (await a.get('a-task')).status == ScheduledTaskStatus.RUNNING


async def test_unbound_legacy_tasks_are_visible_as_count_but_never_adopted(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db', workspace=tmp_path / 'project')
    await store.initialize()
    now = datetime.now(UTC)
    await store.add(ScheduledTask.once('legacy', 'unknown project', now))
    with store._connect() as db:
        db.execute('UPDATE scheduled_tasks SET workspace = NULL')
    assert await store.unbound_count() == 1
    assert await store.claim_due(now, 2) == ()

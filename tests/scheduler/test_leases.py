from datetime import UTC, datetime, timedelta

from miniclaw.scheduler.store import SchedulerStore
from miniclaw.scheduler.types import ScheduledTask, ScheduledTaskStatus


async def test_live_lease_survives_recovery_then_stale_worker_cannot_finish(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    now = datetime.now(UTC)
    await store.add(ScheduledTask.once('job', 'work', now, max_attempts=3))
    first, = await store.claim_due(now, 1, owner='first')
    await store.recover_running(completed_child_runs=(), now=now + timedelta(seconds=10))
    assert (await store.get('job')).status == ScheduledTaskStatus.RUNNING
    assert await store.renew_lease('job', owner='first', attempt=first.attempts,
                                   now=now + timedelta(seconds=30))
    await store.recover_running(completed_child_runs=(), now=now + timedelta(seconds=100))
    second, = await store.claim_due(now + timedelta(seconds=100), 1, owner='second')
    assert second.attempts == 2
    assert not await store.finish('job', child_run_id='stale', succeeded=True,
                                 error=None, owner='first', attempt=first.attempts)
    assert await store.finish('job', child_run_id='current', succeeded=True,
                              error=None, owner='second', attempt=second.attempts)


async def test_expired_task_cannot_exceed_retry_limit(tmp_path):
    store = SchedulerStore(tmp_path / 'jobs.db')
    await store.initialize()
    now = datetime.now(UTC)
    await store.add(ScheduledTask.once('job', 'work', now, max_attempts=1))
    await store.claim_due(now, 1, owner='worker')
    await store.recover_running(completed_child_runs=(), now=now + timedelta(seconds=61))
    assert (await store.get('job')).status == ScheduledTaskStatus.FAILED

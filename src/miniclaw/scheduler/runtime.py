import asyncio
import contextlib
from typing import Protocol
from uuid import uuid4

from miniclaw.scheduler.store import SchedulerStore
from miniclaw.scheduler.types import ScheduledTask, ScheduledTaskStatus, TaskRunResult


class Clock(Protocol):
    def now(self): ...


class TaskRunner(Protocol):
    async def run(self, task: ScheduledTask) -> TaskRunResult: ...


class SchedulerRuntime:
    def __init__(
        self,
        store: SchedulerStore,
        runner: TaskRunner,
        clock: Clock,
        *,
        max_concurrency: int,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.clock = clock
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency
        # Identifies which process holds a claimed task, so a stuck runner can
        # be told apart from one that simply has not finished yet.
        self.owner = owner or f"worker-{uuid4().hex}"
        self.active: dict[str, asyncio.Task] = {}

    async def tick(self) -> None:
        await self.store.recover_running(completed_child_runs=(), now=self.clock.now())
        tasks = await self.store.claim_due(
            self.clock.now(),
            self.max_concurrency,
            owner=self.owner,
        )
        await asyncio.gather(*(self._run(task) for task in tasks))

    async def _run(self, task: ScheduledTask) -> None:
        async with self.semaphore:
            runner = asyncio.create_task(self.runner.run(task))
            self.active[task.task_id] = runner
            try:
                while not runner.done():
                    done, _ = await asyncio.wait((runner,), timeout=1)
                    if done:
                        break
                    if not await self.store.renew_lease(
                        task.task_id, owner=self.owner, attempt=task.attempts,
                        now=self.clock.now(),
                    ):
                        return
                result = await runner
            except asyncio.CancelledError:
                if (await self.store.get(task.task_id)).status is ScheduledTaskStatus.CANCELLED:
                    return
                raise
            except Exception:
                await self.store.finish(
                    task.task_id,
                    child_run_id="",
                    succeeded=False,
                    error="scheduled task failed",
                    owner=self.owner, attempt=task.attempts,
                )
                return
            finally:
                self.active.pop(task.task_id, None)
                if not runner.done():
                    runner.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await runner
            await self.store.finish(
                task.task_id,
                child_run_id=result.child_run_id,
                succeeded=result.succeeded,
                error=result.error,
                owner=self.owner, attempt=task.attempts,
            )

    async def cancel(self, task_id: str) -> int:
        count = await self.store.cancel(task_id)
        for active_id, runner in tuple(self.active.items()):
            if (await self.store.get(active_id)).status is ScheduledTaskStatus.CANCELLED:
                runner.cancel()
        return count

    async def run_forever(self, poll_interval: float) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(poll_interval)

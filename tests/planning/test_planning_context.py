from pathlib import Path

from miniclaw.planning.context import plan_context_source
from miniclaw.planning.store import PlanStore
from miniclaw.planning.types import Plan, Task, TaskStatus


async def test_context_contains_goal_current_and_recent_evidence(
    tmp_path: Path,
    actual_execution,
) -> None:
    store = PlanStore(tmp_path / "plans.db")
    await store.initialize()
    await store.create(
        Plan(
            "plan-1",
            "ship release",
            (
                Task(
                    "inspect",
                    "Inspect",
                    (),
                    TaskStatus.OPEN,
                ),
                Task("edit", "Edit", ("inspect",), TaskStatus.OPEN),
            ),
        )
    )
    await store.transition('plan-1', 'inspect', TaskStatus.IN_PROGRESS)
    proof = await actual_execution(store)
    await store.transition('plan-1', 'inspect', TaskStatus.DONE, evidence=(proof,))

    source = plan_context_source(await store.get("plan-1"))

    assert source.source_id == "plan"
    assert "ship release" in source.text
    assert "edit: Edit" in source.text
    assert proof in source.text

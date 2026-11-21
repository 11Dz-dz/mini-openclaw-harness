import json

from miniclaw.planning.store import PlanStore
from miniclaw.planning.types import Plan, Task, TaskStatus
from miniclaw.tools.types import (
    RegisteredTool,
    ToolCapabilities,
    ToolContext,
    ToolSpec,
)


def make_planning_tools(store: PlanStore) -> tuple[RegisteredTool, ...]:
    async def create_plan(arguments, context: ToolContext) -> str:
        tasks = tuple(
            Task(
                str(item["task_id"]),
                str(item["title"]),
                tuple(str(value) for value in item.get("depends_on", [])),
                TaskStatus.OPEN,
            )
            for item in arguments["tasks"]
        )
        await store.create(
            Plan(str(arguments["plan_id"]), str(arguments["goal"]), tasks)
        )
        return str(arguments["plan_id"])

    async def list_tasks(arguments, context: ToolContext) -> str:
        plan = await store.get(str(arguments["plan_id"]))
        return json.dumps(
            [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status.value,
                    "evidence": list(task.evidence),
                }
                for task in plan.tasks
            ],
            separators=(",", ":"),
        )

    async def list_todos(arguments, context: ToolContext) -> str:
        todos = await store.list_todos(str(arguments["plan_id"]))
        return json.dumps(
            [
                {
                    "todo_id": todo.todo_id,
                    "title": todo.title,
                    "status": todo.status.value,
                    "priority": todo.priority,
                }
                for todo in todos
            ],
            separators=(",", ":"),
        )

    async def transition(
        arguments,
        target: TaskStatus,
        evidence: tuple[str, ...] = (),
        run_id: str = '',
    ) -> str:
        task = await store.transition(
            str(arguments["plan_id"]),
            str(arguments["task_id"]),
            target,
            evidence=evidence,
            run_id=run_id,
        )
        return task.status.value

    async def start_task(arguments, context: ToolContext) -> str:
        return await transition(arguments, TaskStatus.IN_PROGRESS, run_id=context.run_id)

    async def complete_task(arguments, context: ToolContext) -> str:
        evidence = tuple(str(item) for item in arguments['evidence'])
        target = (TaskStatus.DONE if evidence and all(item.startswith('execution:') for item in evidence)
                  else TaskStatus.REPORTED_DONE)
        return await transition(
            arguments,
            target,
            evidence,
            run_id=context.run_id,
        )

    async def verify_task(arguments, context: ToolContext) -> str:
        return await transition(arguments, TaskStatus.DONE,
            tuple(str(item) for item in arguments['evidence']), run_id=context.run_id)

    async def fail_task(arguments, context: ToolContext) -> str:
        return await transition(
            arguments,
            TaskStatus.FAILED,
            (str(arguments["reason"]),),
        )

    object_schema = {"type": "object", "additionalProperties": False}
    plan_id_schema = {
        **object_schema,
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"],
    }
    task_schema = {
        **object_schema,
        "properties": {
            "plan_id": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["plan_id", "task_id"],
    }
    definitions = (
        (
            "create_plan",
            "Create a validated plan",
            {
                **object_schema,
                "properties": {
                    "plan_id": {"type": "string"},
                    "goal": {"type": "string"},
                    "tasks": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["plan_id", "goal", "tasks"],
            },
            create_plan,
        ),
        ("list_tasks", "List plan tasks", plan_id_schema, list_tasks),
        ("list_todos", "List plan todos", plan_id_schema, list_todos),
        ("start_task", "Start one task", task_schema, start_task),
        (
            "complete_task",
            "Report completion. Text claims remain reported_done; DONE requires successful "
            "execution: IDs emitted by tools while this task is active in this run.",
            {
                **task_schema,
                "properties": {
                    **task_schema["properties"],
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["plan_id", "task_id", "evidence"],
            },
            complete_task,
        ),
        (
            "verify_task", "Verify a reported task using execution: IDs from this task and run",
            {**task_schema, 'properties': {**task_schema['properties'],
                'evidence': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}},
             'required': ['plan_id', 'task_id', 'evidence']}, verify_task,
        ),
        (
            "fail_task",
            "Fail one task with a reason",
            {
                **task_schema,
                "properties": {
                    **task_schema["properties"],
                    "reason": {"type": "string"},
                },
                "required": ["plan_id", "task_id", "reason"],
            },
            fail_task,
        ),
    )
    return tuple(
        RegisteredTool(
            ToolSpec(name, description, schema, ToolCapabilities(side_effects=True)),
            handler,
        )
        for name, description, schema, handler in definitions
    )

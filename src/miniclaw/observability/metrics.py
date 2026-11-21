from miniclaw.agent.loop import (
    ApprovalRequested,
    ApprovalResolved,
    ModelStreamStarted,
    ModelUsage,
    RunCompleted,
    RunEnded,
)
from miniclaw.core.events import EventEnvelope
from miniclaw.observability.types import RunMetrics


def metrics_from_events(events: tuple[EventEnvelope, ...]) -> RunMetrics:
    resolutions = [
        event.payload
        for event in events
        if isinstance(event.payload, ApprovalResolved)
    ]
    usages = [event.payload for event in events if isinstance(event.payload, ModelUsage)]
    requests = sum(isinstance(event.payload, ModelStreamStarted) for event in events)
    statuses = [event.payload.status for event in events if isinstance(event.payload, RunEnded)]
    return RunMetrics(
        model_requests=sum(
            isinstance(event.payload, ModelStreamStarted) for event in events
        ),
        input_tokens=(sum(item.input_tokens for item in usages) if requests and len(usages) == requests else None),
        output_tokens=(sum(item.output_tokens for item in usages) if requests and len(usages) == requests else None),
        approval_requested=sum(
            isinstance(event.payload, ApprovalRequested) for event in events
        ),
        approval_approved=sum(item.approved for item in resolutions),
        approval_denied=sum(not item.approved for item in resolutions),
        final_status=(
            statuses[-1] if statuses else "completed"
            if any(isinstance(event.payload, RunCompleted) for event in events)
            else None
        ),
    )

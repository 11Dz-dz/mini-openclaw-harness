from miniclaw.agent.events import (
    ApprovalRequested,
    ApprovalResolved,
    ArtifactsReady,
    ContextBuilt,
    ModelResponseFinished,
    ModelStreamStarted,
    ModelUsage,
    ReasoningRecovery,
    RunCompleted,
    RunEnded,
    VerificationStalled,
    WorkspaceVerified,
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
    responses = [event.payload for event in events if isinstance(event.payload, ModelResponseFinished)]
    truncated = [item for item in responses if item.reason == "length"]
    first_ready = next((event.payload.turn for event in events if isinstance(event.payload, ArtifactsReady)), None)
    recoveries = [event.payload.recovered for event in events if isinstance(event.payload, ReasoningRecovery)]
    return RunMetrics(
        model_requests=requests,
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
        first_artifact_turn=first_ready,
        truncated_responses=len(truncated),
        truncations_without_tool_fragments=sum(not item.tool_fragments for item in truncated),
        history_compaction_rounds=sum(isinstance(event.payload, ContextBuilt) and any(
            source.startswith("message:") for source in event.payload.dropped_source_ids) for event in events),
        reasoning_tokens=(sum(item.reasoning_tokens for item in responses)
                          if requests and len(responses) == requests
                          and all(item.reasoning_tokens is not None for item in responses) else None),
        workspace_verifications=sum(isinstance(event.payload, WorkspaceVerified) for event in events),
        verification_stalls=sum(isinstance(event.payload, VerificationStalled) for event in events),
        reasoning_recovery_requests=recoveries.count(False),
        reasoning_recoveries=recoveries.count(True),
    )

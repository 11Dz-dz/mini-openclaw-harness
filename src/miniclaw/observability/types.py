from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunMetrics:
    model_requests: int
    input_tokens: int | None
    output_tokens: int | None
    approval_requested: int
    approval_approved: int
    approval_denied: int
    final_status: str | None
    first_artifact_turn: int | None = None
    truncated_responses: int = 0
    truncations_without_tool_fragments: int = 0
    history_compaction_rounds: int = 0
    reasoning_tokens: int | None = None

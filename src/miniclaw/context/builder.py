import json
from collections.abc import Iterable

from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import (
    AgentContext,
    ContextBudget,
    ContextBudgetExceeded,
    ContextSource,
)
from miniclaw.core.messages import Message
from miniclaw.model.encoding import encode_message

DEFAULT_CONTEXT_PRIORITIES = {
    "system": 100,
    "tools": 90,
    "plan": 80,
    "recent_messages": 70,
    "memory": 60,
    "older_messages": 30,
}


class ContextBuilder:
    def __init__(self, estimator: CharacterTokenEstimator) -> None:
        self.estimator = estimator

    def build(
        self,
        sources: Iterable[ContextSource],
        budget: ContextBudget,
    ) -> AgentContext:
        indexed = tuple(enumerate(sources))
        groups = {}
        for index, source in indexed:
            groups.setdefault(source.group_id or f"source:{index}", []).append((index, source))
        grouped = tuple(groups.values())
        required = tuple(group for group in grouped if any(s.required for _, s in group))
        optional = sorted(
            (group for group in grouped if not any(s.required for _, s in group)),
            key=lambda group: (
                -max(s.priority for _, s in group),
                -group[0][0] if group[0][1].source_id.startswith("message:") else group[0][0],
            ),
        )
        selected_indices: set[int] = set()
        used = 0
        for group in required:
            used += sum(self.estimator.estimate(s.token_text or s.text) for _, s in group)
            selected_indices.update(index for index, _ in group)
        if used > budget.input_limit:
            raise ContextBudgetExceeded("required context exceeds input budget")
        for group in optional:
            cost = sum(self.estimator.estimate(s.token_text or s.text) for _, s in group)
            if used + cost <= budget.input_limit:
                used += cost
                selected_indices.update(index for index, _ in group)
        items = tuple(
            source for index, source in indexed if index in selected_indices
        )
        dropped = tuple(
            source.source_id
            for index, source in indexed
            if index not in selected_indices
        )
        return AgentContext(items, used, dropped)


def sources_from_messages(messages: tuple[Message, ...]) -> tuple[ContextSource, ...]:
    sources = []
    recent_start = max(0, len(messages) - 4)
    latest_user = max((i for i, message in enumerate(messages)
                       if message.role == "user"), default=-1)
    turn_start = -1
    for index, message in enumerate(messages):
        if message.role == "user" or (index > latest_user >= 0 and message.role == "assistant"):
            turn_start = index
        # Count exactly the content sent to the model, including tool output,
        # JSON arguments, call IDs and protocol fields.
        text = json.dumps(encode_message(message), ensure_ascii=False,
                          separators=(",", ":"))
        category = "recent_messages" if index >= recent_start else "older_messages"
        sources.append(
            ContextSource(
                f"message:{index}",
                text,
                DEFAULT_CONTEXT_PRIORITIES[category],
                required=(message.role == "system" or index == latest_user),
                group_id=(f"system:{index}" if message.role == "system"
                          else f"turn:{turn_start}"),
            )
        )
    return tuple(sources)

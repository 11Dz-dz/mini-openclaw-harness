import json
from dataclasses import replace

from miniclaw.context.builder import ContextBuilder
from miniclaw.context.history import compact_dropped_history, select_request_history
from miniclaw.context.types import AgentContext, ContextBudget, ContextSource
from miniclaw.core.messages import Message, ModelRequest, TextContent
from miniclaw.model.encoding import encode_message, encode_request


def build_request_context(
    request: ModelRequest,
    round_sources: tuple[ContextSource, ...],
    feedback_message: Message | None,
    builder: ContextBuilder | None,
    budget: ContextBudget | None,
) -> tuple[ModelRequest, AgentContext | None, tuple[ContextSource, ...]]:
    request_messages = request.messages
    context_snapshot = None
    selected_extra = round_sources
    if builder is not None and budget is not None:
        overhead = builder.estimator.estimate(json.dumps(
            encode_request(replace(request, messages=()), stream=True),
            ensure_ascii=False, separators=(",", ":")))
        history = request_messages
        budget_sources = tuple(replace(source, token_text=json.dumps(encode_message(
                feedback_message if source.source_id == "harness:feedback-tail"
                else Message("system", (TextContent(source.text),))),
                ensure_ascii=False)) for source in round_sources)
        context_snapshot, request_messages = select_request_history(
            builder, history, budget_sources,
            ContextBudget(budget.total - overhead,
                          budget.reserve_output),
        )
        excerpt = compact_dropped_history(
            history, set(context_snapshot.dropped_source_ids),
            builder.estimator,
            min(2048, budget.input_limit - overhead
                - context_snapshot.estimated_tokens),
        )
        if excerpt is not None:
            encoded = json.dumps(encode_message(Message('system', (TextContent(excerpt.text),))),
                                 ensure_ascii=False)
            excerpt_cost = builder.estimator.estimate(encoded)
            if (context_snapshot.estimated_tokens + excerpt_cost + overhead
                    <= budget.input_limit):
                context_snapshot = replace(context_snapshot,
                    items=(*context_snapshot.items, excerpt),
                    estimated_tokens=context_snapshot.estimated_tokens + excerpt_cost)
        context_snapshot = replace(context_snapshot,
            estimated_tokens=context_snapshot.estimated_tokens + overhead)
        selected_indices = {
            int(item.source_id.removeprefix("message:"))
            for item in context_snapshot.items
            if item.source_id.startswith("message:")
        }
        selected_extra = tuple(
            item
            for item in context_snapshot.items
            if not item.source_id.startswith("message:")
        )
        request_messages = tuple(
            message
            for index, message in enumerate(request_messages)
            if index in selected_indices
        )
    head_sources = tuple(item for item in selected_extra if item.source_id != "harness:feedback-tail")
    if head_sources:
        request_messages = (
            Message(
                "system",
                tuple(
                TextContent(item.text) for item in head_sources
                ),
            ),
            *request_messages,
        )
    if feedback_message is not None:
        request_messages = (*request_messages, feedback_message)
    return replace(request, messages=request_messages), context_snapshot, head_sources

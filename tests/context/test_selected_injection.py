from miniclaw.agent.loop import AgentLoop, ContextInjected
from miniclaw.context.builder import ContextBuilder
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget, ContextSource
from miniclaw.core.messages import ResponseCompleted
from miniclaw.model.fake import FakeModel


async def test_injection_events_report_only_sources_actually_sent_to_model():
    class Provider:
        async def build(self, prompt):
            return (ContextSource('memory:large', 'x' * 10000, 60),
                    ContextSource('memory:small', 'useful small fact', 60))

    model = FakeModel((ResponseCompleted('stop'),))
    result = await AgentLoop(model, 'fake',
        context_builder=ContextBuilder(CharacterTokenEstimator()),
        context_budget=ContextBudget(500, 50), context_providers=(Provider(),)).run('go')
    assert [e.payload.source_ids for e in result.events if isinstance(e.payload, ContextInjected)] == [
        ('memory:small',)]
    assert 'useful small fact' in repr(model.requests[0].messages)
    assert 'x' * 100 not in repr(model.requests[0].messages)

import json

import httpx

from miniclaw.agent.limits import RunLimits
from miniclaw.agent.loop import AgentLoop
from miniclaw.core.messages import ResponseCompleted, Usage
from miniclaw.model.fake import FakeModel
from miniclaw.model.openai_compat import OpenAICompatibleClient
from miniclaw.observability.metrics import metrics_from_events


async def test_real_reported_usage_and_terminal_status_reach_metrics():
    result = await AgentLoop(FakeModel((ResponseCompleted('stop', Usage(12, 3)),)), 'fake').run('go')
    metrics = metrics_from_events(result.events)
    assert (metrics.input_tokens, metrics.output_tokens, metrics.final_status) == (12, 3, 'completed')
    exhausted = await AgentLoop(FakeModel(()), 'fake', limits=RunLimits(0, 0)).run('go')
    assert metrics_from_events(exhausted.events).final_status == 'exhausted'
    failed = await AgentLoop(FakeModel((ResponseCompleted('length'),)), 'fake').run('go')
    assert metrics_from_events(failed.events).final_status == 'failed'


async def test_separate_trailing_usage_chunk_is_not_lost_when_finish_arrives_first():
    body = ('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
            'data: [DONE]\n\n')

    def handler(request):
        assert json.loads(request.content)['stream_options'] == {'include_usage': True}
        return httpx.Response(200, text=body)

    client = OpenAICompatibleClient('https://fixture.invalid/v1', 'synthetic-key',
        transport=httpx.MockTransport(handler))
    try:
        result = await AgentLoop(client, 'fake').run('go')
    finally:
        await client.aclose()
    metrics = metrics_from_events(result.events)
    assert (metrics.input_tokens, metrics.output_tokens) == (7, 2)

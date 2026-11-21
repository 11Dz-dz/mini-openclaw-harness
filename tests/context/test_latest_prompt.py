import pytest

from miniclaw.context.builder import ContextBuilder, sources_from_messages
from miniclaw.context.tokens import CharacterTokenEstimator
from miniclaw.context.types import ContextBudget, ContextBudgetExceeded
from miniclaw.core.messages import Message, TextContent


def test_latest_user_request_cannot_be_replaced_by_older_shorter_history():
    messages = (Message('user', (TextContent('old'),)),
                Message('assistant', (TextContent('old answer'),)),
                Message('user', (TextContent('current request ' * 100),)))
    with pytest.raises(ContextBudgetExceeded, match='required context'):
        ContextBuilder(CharacterTokenEstimator()).build(
            sources_from_messages(messages), ContextBudget(100, 10))


def test_system_and_latest_user_are_kept_before_old_history():
    messages = (Message('system', (TextContent('always'),)),
                Message('assistant', (TextContent('old ' * 1000),)),
                Message('user', (TextContent('current'),)))
    result = ContextBuilder(CharacterTokenEstimator()).build(
        sources_from_messages(messages), ContextBudget(100, 10))
    assert [source.source_id for source in result.items] == ['message:0', 'message:2']

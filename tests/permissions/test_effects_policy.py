import pytest

from miniclaw.permissions.policy import DefaultPermissionPolicy
from miniclaw.permissions.types import PermissionDecision
from miniclaw.tools.types import RiskLevel, ToolCapabilities


@pytest.mark.parametrize('capabilities', [
    ToolCapabilities(side_effects=True), ToolCapabilities(idempotent=False),
    ToolCapabilities(risk_level=RiskLevel.HIGH),
    ToolCapabilities(filesystem='read', side_effects=True),
])
def test_side_effects_and_risk_are_not_mistaken_for_pure_computation(capabilities):
    result = DefaultPermissionPolicy().evaluate(
        tool_name='custom', capabilities=capabilities, arguments={})
    assert result.decision == PermissionDecision.ASK


def test_invalid_capabilities_are_denied_before_an_early_approval_branch():
    result = DefaultPermissionPolicy().evaluate(tool_name='invalid',
        capabilities=ToolCapabilities(filesystem='unknown', network='unrestricted'),
        arguments={})
    assert result.decision == PermissionDecision.DENY


def test_internal_state_trust_does_not_authorize_scheduling_or_high_risk_changes():
    policy = DefaultPermissionPolicy(trusted_state_tools=('remember',))
    assert policy.evaluate(tool_name='remember', capabilities=ToolCapabilities(
        side_effects=True), arguments={}).decision == PermissionDecision.ALLOW
    assert policy.evaluate(tool_name='schedule_task', capabilities=ToolCapabilities(
        side_effects=True, idempotent=False, risk_level=RiskLevel.HIGH),
        arguments={}).decision == PermissionDecision.ASK

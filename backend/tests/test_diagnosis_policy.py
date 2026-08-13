import pytest
from app.diagnosis.policy import DiagnosisPlanPolicyError,enforce_plan_action
from app.diagnosis.types import PlanAction

def test_policy_overrides_fake_low_risk_claim():
    a=PlanAction('REQUEST_MULTI_POINT_PCAP','x','L0',True,{})
    x=enforce_plan_action(a)
    assert x.risk_level=='USER' and x.auto_execute is False

def test_only_voip_basic_collect_profile_can_auto_run():
    x=enforce_plan_action(PlanAction('COLLECT_PROFILE','x','L0',True,{'profile_id':'dangerous_profile'}))
    assert x.risk_level=='L1' and x.auto_execute is False

def test_unknown_high_level_action_rejected():
    with pytest.raises(DiagnosisPlanPolicyError): enforce_plan_action(PlanAction('RUN_SHELL','x','L0',True,{'command':'rm -rf /'}))

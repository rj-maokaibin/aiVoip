from app.contracts.enums import ConfirmationPolicy, ExperimentVariant
from app.experiments.profile import ExperimentProfileRegistry


def test_sip_registration_egress_block_aba_profile_is_fail_closed():
    profile = ExperimentProfileRegistry().get("SIP_REGISTRATION_EGRESS_BLOCK_ABA").definition

    assert profile.hypothesis_codes == ["SIP_REGISTRATION_PATH_FAILURE"]
    assert profile.reproduction_profile_id == "REGISTER_FAILURE"
    assert profile.independent_variable == "external.sip_egress_blocked"
    assert profile.target_finding == "SIP_REGISTRATION_FAILED"
    assert profile.confirmation_policy == ConfirmationPolicy.ABA_REQUIRED
    assert profile.sequence == [ExperimentVariant.A1, ExperimentVariant.B, ExperimentVariant.A2]
    assert profile.external_action_required is True
    assert profile.expected_change_paths == ["external.sip_egress_blocked"]
    assert "device.serial" in profile.must_equal_paths
    assert "software.version" in profile.must_equal_paths
    assert "voice.gateway_ip" in profile.must_equal_paths
    assert "禁止修改PBX" in profile.external_action_instructions
    assert "必须删除精确规则" in profile.external_action_instructions

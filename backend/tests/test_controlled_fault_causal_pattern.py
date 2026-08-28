from types import SimpleNamespace

from app.contracts.enums import (
    CallVerdict,
    CausalConclusionState,
    ConfirmationPolicy,
    ExperimentVariant,
)
from app.experiments.causal import CausalConfirmationEngine
from app.experiments.profile import ExperimentProfileDefinition


def _profile(pattern):
    return ExperimentProfileDefinition(
        id="CONTROLLED_SIP_EGRESS_BLOCK_ABA",
        name="controlled SIP egress block",
        hypothesis_codes=["DUT_SIP_GATEWAY_EGRESS_BLOCK"],
        reproduction_profile_id="REGISTER_FAILURE",
        independent_variable="controlled_fault.sip_gateway_egress_block",
        target_finding="SIP_REGISTRATION_FAILURE",
        confirmation_policy=ConfirmationPolicy.ABA_REQUIRED,
        sequence=[ExperimentVariant.A1, ExperimentVariant.B, ExperimentVariant.A2],
        causal_pattern=pattern,
        external_action_required=False,
        controlled_action_id="SIP_GATEWAY_EGRESS_BLOCK_V1",
        expected_change_paths=["controlled_fault.sip_gateway_egress_block"],
        must_equal_paths=["device.identity", "voice.gateway"],
    )


def _run(run_id, no, variant, target):
    return SimpleNamespace(
        id=run_id,
        run_no=no,
        variant=variant.value,
        status="COMPLETED",
        target_finding_present=target,
        target_verdict=CallVerdict.MATCH.value if target else CallVerdict.NO_MATCH.value,
    )


def _cmp(cmp_id, a, b):
    return SimpleNamespace(
        id=cmp_id,
        baseline_run_id=a.id,
        variant_run_id=b.id,
        status="COMPARABLE",
    )


def test_inverse_aba_confirms_fault_injection_only_after_restore():
    a1 = _run("a1", 1, ExperimentVariant.A1, False)
    b = _run("b", 2, ExperimentVariant.B, True)
    a2 = _run("a2", 3, ExperimentVariant.A2, False)
    comparisons = [_cmp("ab", a1, b), _cmp("aa", a1, a2)]
    engine = CausalConfirmationEngine()

    ab = engine.evaluate(
        profile=_profile("A1_CONTROL_B_TARGET_A2_CONTROL"),
        runs=[a1, b],
        comparisons=[comparisons[0]],
    )
    assert ab.state == CausalConclusionState.STRONGLY_SUPPORTED
    assert ab.rationale["pattern_ab"] is True
    assert ab.rationale["pattern_aba"] is False

    aba = engine.evaluate(
        profile=_profile("A1_CONTROL_B_TARGET_A2_CONTROL"),
        runs=[a1, b, a2],
        comparisons=comparisons,
    )
    assert aba.state == CausalConclusionState.ROOT_CAUSE_CONFIRMED
    assert aba.rationale["causal_pattern"] == "A1_CONTROL_B_TARGET_A2_CONTROL"
    assert aba.rationale["pattern_aba"] is True


def test_inverse_aba_does_not_confirm_when_fault_does_not_change_registration():
    a1 = _run("a1", 1, ExperimentVariant.A1, False)
    b = _run("b", 2, ExperimentVariant.B, False)
    a2 = _run("a2", 3, ExperimentVariant.A2, False)
    decision = CausalConfirmationEngine().evaluate(
        profile=_profile("A1_CONTROL_B_TARGET_A2_CONTROL"),
        runs=[a1, b, a2],
        comparisons=[_cmp("ab", a1, b), _cmp("aa", a1, a2)],
    )
    assert decision.state == CausalConclusionState.CONTRADICTED


def test_legacy_aba_direction_remains_backward_compatible():
    profile = ExperimentProfileDefinition(
        id="LEGACY_SWAP",
        name="legacy swap",
        hypothesis_codes=["X"],
        reproduction_profile_id="AUDIO_NOISE",
        independent_variable="phone.id",
        target_finding="PERIODIC_INTERFERENCE",
        confirmation_policy=ConfirmationPolicy.ABA_REQUIRED,
        sequence=[ExperimentVariant.A1, ExperimentVariant.B, ExperimentVariant.A2],
        external_action_required=True,
        expected_change_paths=["phone.id"],
    )
    a1 = _run("a1", 1, ExperimentVariant.A1, True)
    b = _run("b", 2, ExperimentVariant.B, False)
    a2 = _run("a2", 3, ExperimentVariant.A2, True)
    decision = CausalConfirmationEngine().evaluate(
        profile=profile,
        runs=[a1, b, a2],
        comparisons=[_cmp("ab", a1, b), _cmp("aa", a1, a2)],
    )
    assert profile.causal_pattern == "A1_TARGET_B_CONTROL_A2_TARGET"
    assert decision.state == CausalConclusionState.ROOT_CAUSE_CONFIRMED

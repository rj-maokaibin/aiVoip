from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    CallVerdict, CaseStatus, CausalConclusionState, DiagnosticQuestionState,
    EnvironmentComparisonStatus, ExperimentRunStatus, FixActionType,
    FixVerificationStatus, HypothesisState,
)
from app.core.errors import AppError
from app.db.base import Base
from app.db.models import (
    Case, CaseDevice, CausalAssessment, DiagnosticExperiment, DiagnosticQuestion,
    EnvironmentComparison, Evidence, ExperimentEnvironmentSnapshot, ExperimentRun,
    FixVerificationRun, Hypothesis, HypothesisRevision, ReproductionCall,
)
from app.experiments.environment import EnvironmentComparator
from app.experiments.fix_verification import FixVerificationService
from app.experiments.orchestrator import DiagnosticExperimentOrchestrator
from app.experiments.profile import ExperimentProfileRegistry
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.question_graph import DiagnosticQuestionGraph, DiagnosticQuestionRegistry
from app.reproduction.quick import QuickAnalysisInput

ROOT = Path(__file__).resolve().parents[2]


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, no: str, *, status: str = CaseStatus.ANALYZING.value) -> tuple[Case, CaseDevice]:
    case = Case(case_no=no, summary="M6.2 Phase C3 deterministic diagnostic experiment", status=status)
    db.add(case)
    db.flush()
    device = CaseDevice(
        case_id=case.id,
        ip="198.51.100.10",
        ssh_port=22,
        sn=f"SN-{no}",
        username="admin",
        device_info={
            "model": "MOCK-VOIP",
            "software_version": "ReyeeOS-MOCK-1.0",
            "boot_id": "boot-stable",
            "uptime_seconds": 600,
            "fxs_port": "1",
        },
    )
    db.add(device)
    db.flush()
    return case, device


def _reproduction(tmp_path: Path, suffix: str) -> ReproductionOrchestrator:
    pipe = ReproductionCapturePipeline(
        root=tmp_path / f"capture-{suffix}",
        storage=FilesystemObjectStorage(tmp_path / f"objects-{suffix}"),
    )
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(ROOT / "profiles"),
        platform=MockReproductionPlatform(),
        capture_pipeline=pipe,
    )


def _experiment_orch(tmp_path: Path, suffix: str) -> DiagnosticExperimentOrchestrator:
    return DiagnosticExperimentOrchestrator(
        registry=ExperimentProfileRegistry(ROOT / "profiles" / "experiments"),
        reproduction=_reproduction(tmp_path, suffix),
        questions=DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT / "profiles" / "questions")),
    )


def _evidence(db: Session, case: Case, suffix: str = "q") -> Evidence:
    row = Evidence(
        case_id=case.id,
        type="TEST_L1",
        source="TEST",
        kind="RAW",
        source_scope="CASE",
        level="L1",
        completeness="COMPLETE",
        filename=f"{suffix}.json",
        object_key=f"tests/{case.id}/{suffix}.json",
        size_bytes=2,
        sha256=(suffix[0] if suffix else "a") * 64,
        content_type="application/json",
    )
    db.add(row)
    db.flush()
    return row


def _hypothesis(db: Session, case: Case, code: str = "LOCAL_CAPTURE_PERIODIC_INTERFERENCE") -> Hypothesis:
    row = Hypothesis(
        case_id=case.id,
        code=code,
        title="deterministic experiment hypothesis",
        fault_domain="LOCAL_AUDIO_PATH",
        status=HypothesisState.SUPPORTED.value,
        confidence=8000,
        rationale="current-case deterministic findings support experiment",
    )
    db.add(row)
    db.flush()
    return row


CALL_CONTEXT = {
    "fxs_port": "1",
    "codec": "PCMA/8000",
    "ptime": 20,
    "direction": "sendrecv",
    "remote_endpoint": "192.0.2.1:10000",
    "called_number": "10086",
}


def _external(power_supply: str) -> dict:
    return {
        "power_supply_id": power_supply,
        "phone_id": "PHONE-A",
        "line_id": "LINE-A",
    }


def _run_call(
    db: Session,
    exp_orch: DiagnosticExperimentOrchestrator,
    experiment: DiagnosticExperiment,
    *,
    external_state: dict,
    verdict: CallVerdict,
    findings: tuple[str, ...],
    environment_overrides: dict | None = None,
):
    run = exp_orch.plan_next_run(db, experiment=experiment)
    assert run is not None
    if run.external_action_required:
        exp_orch.complete_external_action(db, run=run)
    session = exp_orch.start_reproduction(
        db,
        run=run,
        external_state=external_state,
        call_context=CALL_CONTEXT,
        environment_overrides=environment_overrides,
    )
    rep = exp_orch.reproduction
    rep.record_activity(db, session=session, relative_ms=100)
    call = rep.bind_call(db, session=session, relative_ms=300)
    call, _ = rep.end_call(
        db,
        session=session,
        call_id=call.id,
        relative_ms=2500,
        signal=QuickAnalysisInput(verdict, findings=findings),
    )
    assessment = exp_orch.attach_result(
        db,
        run=run,
        session_id=session.id,
        call_id=call.id,
        external_state=external_state,
        call_context=CALL_CONTEXT,
        environment_overrides=environment_overrides,
    )
    return run, session, call, assessment


def _setup_power_experiment(db: Session, tmp_path: Path, suffix: str = "power"):
    case, _ = _case(db, f"C3-{suffix}")
    hypothesis = _hypothesis(db, case)
    qgraph = DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT / "profiles" / "questions"))
    question = qgraph.ensure_question(
        db,
        case_id=case.id,
        question_key="AUDIO_NOISE_ROOT_CAUSE",
        state=DiagnosticQuestionState.IN_PROGRESS,
    )
    exp_orch = _experiment_orch(tmp_path, suffix)
    experiment = exp_orch.create_experiment(
        db,
        case_id=case.id,
        profile_id="POWER_SUPPLY_AB",
        hypothesis_id=hypothesis.id,
        question_id=question.id,
    )
    return case, hypothesis, question, experiment, exp_orch


def test_diagnostic_question_registry_is_dag_and_selects_highest_information_gain():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case(db, "C3-Q")
        graph = DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT / "profiles" / "questions"))
        generic = graph.ensure_question(db, case_id=case.id, question_key="GENERIC_SYMPTOM_CLASSIFICATION")
        graph.ensure_question(db, case_id=case.id, question_key="VOIP_FAULT_DOMAIN")
        selected = graph.select_next(db, case_id=case.id)
        assert selected.id == generic.id
        ev = _evidence(db, case)
        children = graph.answer(
            db,
            question=generic,
            answer={"route": "AUDIO_NOISE", "findings": []},
            route="AUDIO_NOISE",
            evidence_refs=[{"evidence_id": ev.id, "level": "L1"}],
        )
        assert [x.question_key for x in children] == ["AUDIO_NOISE_FAULT_LAYER"]
        assert children[0].parent_question_id == generic.id
        assert graph.select_next(db, case_id=case.id).question_key == "AUDIO_NOISE_FAULT_LAYER"


def test_question_answer_rejects_missing_required_deterministic_finding():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case(db, "C3-Q-NEG")
        graph = DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT / "profiles" / "questions"))
        q = graph.ensure_question(db, case_id=case.id, question_key="AUDIO_NOISE_FAULT_LAYER")
        ev = _evidence(db, case, "e")
        with pytest.raises(AppError) as exc:
            graph.answer(db, question=q, answer={"findings": ["ACTIVE_MEDIA_WINDOW"]}, evidence_refs=[{"evidence_id": ev.id, "level": "L1"}])
        assert exc.value.code == "DIAGNOSTIC_QUESTION_EVIDENCE_INSUFFICIENT"
        assert q.state != DiagnosticQuestionState.ANSWERED.value


def test_experiment_registry_loads_seven_frozen_profiles_without_real_commands():
    registry = ExperimentProfileRegistry(ROOT / "profiles" / "experiments")
    ids = {x.definition.id for x in registry.list()}
    assert ids == {
        "PHONE_SWAP_AB",
        "LINE_SWAP_AB",
        "FXS_PORT_SWAP_AB",
        "POWER_SUPPLY_AB",
        "DEVICE_SWAP_AB",
        "POST_REBOOT_FIRST_CALL",
        "SIP_REGISTRATION_EGRESS_BLOCK_ABA",
    }
    for item in registry.list():
        blob = item.definition.model_dump_json().lower()
        assert "shell" not in blob and "ssh_command" not in blob and "aim_command" not in blob
        assert len(item.checksum) == 64


def test_environment_comparator_classifies_expected_soft_and_hard_drift():
    profile = ExperimentProfileRegistry(ROOT / "profiles" / "experiments").get("POWER_SUPPLY_AB").definition
    comparator = EnvironmentComparator()
    baseline = {
        "device": {"serial": "SN1"}, "software": {"version": "V1"}, "boot": {"uptime_seconds": 100},
        "voice": {"voice_vlan_id": "100", "gateway_ip": "192.0.2.1", "fxs_port": "1"},
        "call": {"codec": "PCMA/8000", "called_number": "10086", "remote_endpoint": "A"},
        "external": {"power_supply_id": "BAD", "phone_id": "P", "line_id": "L"},
    }
    variant = {
        "device": {"serial": "SN1"}, "software": {"version": "V1"}, "boot": {"uptime_seconds": 120},
        "voice": {"voice_vlan_id": "100", "gateway_ip": "192.0.2.1", "fxs_port": "1"},
        "call": {"codec": "PCMA/8000", "called_number": "10086", "remote_endpoint": "B"},
        "external": {"power_supply_id": "GOOD", "phone_id": "P", "line_id": "L"},
    }
    decision = comparator.evaluate(profile=profile, baseline=baseline, variant=variant)
    assert decision.status == EnvironmentComparisonStatus.COMPARABLE_WITH_SOFT_DRIFT
    assert {x["path"] for x in decision.expected_changes} == {"external.power_supply_id"}
    assert {x["path"] for x in decision.soft_drift} == {"boot.uptime_seconds", "call.remote_endpoint"}
    bad = {**variant, "software": {"version": "V2"}}
    decision2 = comparator.evaluate(profile=profile, baseline=baseline, variant=bad)
    assert decision2.status == EnvironmentComparisonStatus.NOT_COMPARABLE
    assert any(x["path"] == "software.version" for x in decision2.hard_drift)


def test_ab_under_aba_required_only_strongly_supports_and_aba_confirms_root_cause(tmp_path):
    eng = _engine()
    with Session(eng) as db:
        case, hypothesis, question, experiment, exp_orch = _setup_power_experiment(db, tmp_path, "aba")
        a1, _, _, a1_assessment = _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        assert a1.variant == "A1" and a1_assessment.state == CausalConclusionState.INCONCLUSIVE.value
        b, _, _, ab = _run_call(
            db, exp_orch, experiment, external_state=_external("GOOD"), verdict=CallVerdict.NO_MATCH,
            findings=("ACTIVE_MEDIA_WINDOW",),
        )
        assert b.variant == "B"
        assert ab.state == CausalConclusionState.STRONGLY_SUPPORTED.value
        assert case.status == CaseStatus.ANALYZING.value
        assert hypothesis.status == HypothesisState.STRONGLY_SUPPORTED.value
        a2, _, _, aba = _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        assert a2.variant == "A2"
        assert aba.state == CausalConclusionState.ROOT_CAUSE_CONFIRMED.value
        assert case.status == CaseStatus.ROOT_CAUSE_CONFIRMED.value
        assert hypothesis.status == HypothesisState.CONFIRMED.value
        revisions = list(db.scalars(select(HypothesisRevision).where(HypothesisRevision.hypothesis_id == hypothesis.id).order_by(HypothesisRevision.revision_no)))
        assert [x.status for x in revisions] == [HypothesisState.STRONGLY_SUPPORTED.value, HypothesisState.CONFIRMED.value]
        assert question.state == DiagnosticQuestionState.ANSWERED.value
        fix_q = db.scalar(select(DiagnosticQuestion).where(DiagnosticQuestion.case_id == case.id, DiagnosticQuestion.question_key == "FIX_VERIFICATION"))
        assert fix_q is not None and fix_q.parent_question_id == question.id
        pre_count = len(list(db.scalars(select(ExperimentEnvironmentSnapshot).where(ExperimentEnvironmentSnapshot.experiment_id == experiment.id, ExperimentEnvironmentSnapshot.phase == "PRE"))))
        post_count = len(list(db.scalars(select(ExperimentEnvironmentSnapshot).where(ExperimentEnvironmentSnapshot.experiment_id == experiment.id, ExperimentEnvironmentSnapshot.phase == "POST"))))
        assert pre_count == 3 and post_count == 3


def test_hard_drift_marks_variant_invalid_and_retry_can_recover(tmp_path):
    eng = _engine()
    with Session(eng) as db:
        _, _, _, experiment, exp_orch = _setup_power_experiment(db, tmp_path, "retry")
        _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        bad_run, _, _, bad_assessment = _run_call(
            db, exp_orch, experiment, external_state=_external("GOOD"), verdict=CallVerdict.NO_MATCH,
            findings=("ACTIVE_MEDIA_WINDOW",), environment_overrides={"software": {"version": "V2-DRIFT"}},
        )
        assert bad_run.variant == "B" and bad_run.status == ExperimentRunStatus.INVALID.value
        assert bad_assessment.state == CausalConclusionState.NOT_COMPARABLE.value
        comp = db.scalar(select(EnvironmentComparison).where(EnvironmentComparison.variant_run_id == bad_run.id))
        assert comp and comp.status == EnvironmentComparisonStatus.NOT_COMPARABLE.value
        retry = exp_orch.plan_next_run(db, experiment=experiment)
        assert retry is not None and retry.variant == "B" and retry.run_no == 3
        exp_orch.complete_external_action(db, run=retry)
        session = exp_orch.start_reproduction(db, run=retry, external_state=_external("GOOD"), call_context=CALL_CONTEXT)
        exp_orch.reproduction.record_activity(db, session=session, relative_ms=100)
        call = exp_orch.reproduction.bind_call(db, session=session, relative_ms=300)
        call, _ = exp_orch.reproduction.end_call(db, session=session, call_id=call.id, relative_ms=2500, signal=QuickAnalysisInput(CallVerdict.NO_MATCH, findings=("ACTIVE_MEDIA_WINDOW",)))
        assessment = exp_orch.attach_result(db, run=retry, session_id=session.id, call_id=call.id, external_state=_external("GOOD"), call_context=CALL_CONTEXT)
        assert retry.status == ExperimentRunStatus.COMPLETED.value
        assert assessment.state == CausalConclusionState.STRONGLY_SUPPORTED.value


def _verification_call(db: Session, rep: ReproductionOrchestrator, case: Case, *, match: bool, suffix: str):
    session = rep.create_session(db, case_id=case.id, profile_id="AUDIO_NOISE")
    rep.start(db, session=session)
    rep.record_activity(db, session=session, relative_ms=100)
    call = rep.bind_call(db, session=session, relative_ms=300)
    findings = ("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION") if match else ("ACTIVE_MEDIA_WINDOW",)
    call, _ = rep.end_call(
        db, session=session, call_id=call.id, relative_ms=2500,
        signal=QuickAnalysisInput(CallVerdict.MATCH if match else CallVerdict.NO_MATCH, findings=findings),
    )
    if session.state in {"WATCHING", "ACTIVITY_DETECTED"}:
        session.terminal_reason = f"FIX_VERIFY_{suffix}"
        rep.cleanup(db, session=session)
    return session, call


def _fix_env(serial: str, version: str = "ReyeeOS-MOCK-1.0") -> dict:
    return {
        "device": {"serial": serial},
        "software": {"version": version},
        "voice": {"voice_vlan_id": "100", "gateway_ip": "192.0.2.1", "fxs_port": "1"},
        "call": {"codec": "PCMA/8000", "called_number": "10086"},
        "external": {"power_supply_id": "GOOD"},
    }


def test_fix_verification_requires_configured_success_calls_then_resolves_case_and_answers_dag(tmp_path):
    eng = _engine()
    with Session(eng) as db:
        case, hypothesis, _, experiment, exp_orch = _setup_power_experiment(db, tmp_path, "fix")
        a1, base_session, base_call, _ = _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        _run_call(db, exp_orch, experiment, external_state=_external("GOOD"), verdict=CallVerdict.NO_MATCH, findings=("ACTIVE_MEDIA_WINDOW",))
        _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        assert case.status == CaseStatus.ROOT_CAUSE_CONFIRMED.value
        fix_storage = FilesystemObjectStorage(tmp_path / "fix-objects")
        service = FixVerificationService(storage=fix_storage)
        fix = service.create_fix_action(
            db, case_id=case.id, action_type=FixActionType.POWER_SUPPLY_REPLACE,
            description="replace suspect power supply", experiment_id=experiment.id,
            metadata={"allowed_environment_changes": ["external.power_supply_id"]},
        )
        assert case.status == CaseStatus.RESOLVING.value
        verification = service.create_verification(
            db, fix_action_id=fix.id, baseline_session_id=base_session.id, baseline_call_id=base_call.id,
            target_finding="PERIODIC_INTERFERENCE", required_calls=2, max_calls=3,
        )
        rep = _reproduction(tmp_path, "fix-verify")
        v1s, v1c = _verification_call(db, rep, case, match=False, suffix="1")
        env = _fix_env(f"SN-C3-fix")
        service.evaluate(
            db, verification=verification, verification_session_id=v1s.id, verification_call_id=v1c.id,
            baseline_environment=env, verification_environment=env,
            business_checks={"sip_call_established": True, "rtp_bidirectional": True},
        )
        assert verification.status == FixVerificationStatus.RUNNING.value
        assert verification.verification_call_count == 1 and verification.successful_call_count == 1
        assert case.status == CaseStatus.RESOLVING.value
        v2s, v2c = _verification_call(db, rep, case, match=False, suffix="2")
        service.evaluate(
            db, verification=verification, verification_session_id=v2s.id, verification_call_id=v2c.id,
            baseline_environment=env, verification_environment=env,
            business_checks={"sip_call_established": True, "rtp_bidirectional": True},
        )
        assert verification.status == FixVerificationStatus.FIX_VERIFIED.value
        assert verification.verification_call_count == 2 and verification.successful_call_count == 2
        assert len(verification.evaluations_json) == 2
        assert case.status == CaseStatus.RESOLVED.value
        evidence = db.get(Evidence, verification.evidence_id)
        assert evidence and evidence.type == "FIX_COMPARISON" and evidence.level == "L1"
        fix_q = db.scalar(select(DiagnosticQuestion).where(DiagnosticQuestion.case_id == case.id, DiagnosticQuestion.question_key == "FIX_VERIFICATION"))
        assert fix_q and fix_q.state == DiagnosticQuestionState.ANSWERED.value
        # Same call is idempotent and never creates an extra successful observation.
        service.evaluate(
            db, verification=verification, verification_session_id=v2s.id, verification_call_id=v2c.id,
            baseline_environment=env, verification_environment=env,
            business_checks={"sip_call_established": True, "rtp_bidirectional": True},
        )
        assert verification.verification_call_count == 2


def test_fix_verification_target_reappears_reopens_diagnosis(tmp_path):
    eng = _engine()
    with Session(eng) as db:
        case, hypothesis, _, experiment, exp_orch = _setup_power_experiment(db, tmp_path, "fix-fail")
        _, base_session, base_call, _ = _run_call(
            db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH,
            findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"),
        )
        _run_call(db, exp_orch, experiment, external_state=_external("GOOD"), verdict=CallVerdict.NO_MATCH, findings=("ACTIVE_MEDIA_WINDOW",))
        _run_call(db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH, findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"))
        service = FixVerificationService(storage=FilesystemObjectStorage(tmp_path / "fix-fail-objects"))
        fix = service.create_fix_action(db, case_id=case.id, action_type=FixActionType.POWER_SUPPLY_REPLACE, description="replace power", experiment_id=experiment.id)
        verification = service.create_verification(db, fix_action_id=fix.id, baseline_session_id=base_session.id, baseline_call_id=base_call.id, target_finding="PERIODIC_INTERFERENCE")
        rep = _reproduction(tmp_path, "fix-fail-verify")
        vs, vc = _verification_call(db, rep, case, match=True, suffix="fail")
        env = _fix_env(f"SN-C3-fix-fail")
        service.evaluate(db, verification=verification, verification_session_id=vs.id, verification_call_id=vc.id, baseline_environment=env, verification_environment=env, business_checks={"sip_call_established": True})
        assert verification.status == FixVerificationStatus.FIX_FAILED.value
        assert case.status == CaseStatus.ANALYZING.value


def test_fix_verification_new_blocking_finding_is_regression_not_resolved(tmp_path):
    eng = _engine()
    with Session(eng) as db:
        case, hypothesis, _, experiment, exp_orch = _setup_power_experiment(db, tmp_path, "fix-reg")
        _, base_session, base_call, _ = _run_call(db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH, findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"))
        _run_call(db, exp_orch, experiment, external_state=_external("GOOD"), verdict=CallVerdict.NO_MATCH, findings=("ACTIVE_MEDIA_WINDOW",))
        _run_call(db, exp_orch, experiment, external_state=_external("BAD"), verdict=CallVerdict.MATCH, findings=("ACTIVE_MEDIA_WINDOW", "PERIODIC_INTERFERENCE", "PCM_RTP_CORRELATION"))
        service = FixVerificationService(storage=FilesystemObjectStorage(tmp_path / "fix-reg-objects"))
        fix = service.create_fix_action(db, case_id=case.id, action_type=FixActionType.POWER_SUPPLY_REPLACE, description="replace power", experiment_id=experiment.id)
        verification = service.create_verification(db, fix_action_id=fix.id, baseline_session_id=base_session.id, baseline_call_id=base_call.id, target_finding="PERIODIC_INTERFERENCE")
        rep = _reproduction(tmp_path, "fix-reg-verify")
        vs, vc = _verification_call(db, rep, case, match=False, suffix="reg")
        env = _fix_env(f"SN-C3-fix-reg")
        service.evaluate(
            db, verification=verification, verification_session_id=vs.id, verification_call_id=vc.id,
            baseline_environment=env, verification_environment=env,
            business_checks={"sip_call_established": True}, new_blocking_findings=["NEW_ONE_WAY_AUDIO"],
        )
        assert verification.status == FixVerificationStatus.FIX_REGRESSION.value
        assert case.status == CaseStatus.RESOLVING.value


def test_post_reboot_profile_never_contains_auto_reboot_or_real_device_command():
    loaded = ExperimentProfileRegistry(ROOT / "profiles" / "experiments").get("POST_REBOOT_FIRST_CALL")
    d = loaded.definition
    assert d.confirmation_policy.value == "REPEAT_MATCH"
    assert d.external_action_required is True
    assert d.reproduction_profile_id == "DTMF_LOSS"
    # The experiment describes the external physical/business-impact action but never executes it.
    assert "重启" in d.external_action_instructions
    raw = d.canonical()
    assert not any(key in raw for key in {"command", "shell", "action_id", "ssh_command", "aim_command"})

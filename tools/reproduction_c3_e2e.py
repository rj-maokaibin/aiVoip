#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.enums import (
    CallVerdict, CaseStatus, CausalConclusionState, DiagnosticQuestionState,
    ExperimentRunStatus, FixActionType, FixVerificationStatus, HypothesisState,
)
from app.db.base import Base
from app.db.models import Case, CaseDevice, DiagnosticQuestion, Evidence, Hypothesis
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

CALL_CONTEXT = {
    "fxs_port": "1", "codec": "PCMA/8000", "ptime": 20, "direction": "sendrecv",
    "remote_endpoint": "192.0.2.1:10000", "called_number": "10086",
}


def setup_case(db: Session, no: str, hypothesis_code: str):
    case = Case(case_no=no, summary="M6.2 C3 E2E", status=CaseStatus.ANALYZING.value)
    db.add(case); db.flush()
    dev = CaseDevice(
        case_id=case.id, ip="198.51.100.10", ssh_port=22, sn=f"SN-{no}", username="admin",
        device_info={"model":"MOCK-VOIP","software_version":"V1","boot_id":"boot-1","uptime_seconds":600,"fxs_port":"1"},
    )
    db.add(dev); db.flush()
    hypothesis = Hypothesis(
        case_id=case.id, code=hypothesis_code, title="C3 E2E hypothesis", fault_domain="VOIP",
        status=HypothesisState.SUPPORTED.value, confidence=8000, rationale="deterministic evidence",
    )
    db.add(hypothesis); db.flush()
    return case, dev, hypothesis


def make_reproduction(base: Path, suffix: str):
    pipe = ReproductionCapturePipeline(root=base/f"capture-{suffix}", storage=FilesystemObjectStorage(base/f"objects-{suffix}"))
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(ROOT/'profiles'), platform=MockReproductionPlatform(), capture_pipeline=pipe,
    )


def make_experiment_orch(base: Path, suffix: str):
    return DiagnosticExperimentOrchestrator(
        registry=ExperimentProfileRegistry(ROOT/'profiles'/'experiments'),
        reproduction=make_reproduction(base, suffix),
        questions=DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT/'profiles'/'questions')),
    )


def run_variant(db, orch, experiment, *, external_state, verdict, findings, call_context=None, env_overrides=None):
    run = orch.plan_next_run(db, experiment=experiment)
    assert run is not None
    if run.external_action_required:
        orch.complete_external_action(db, run=run)
    session = orch.start_reproduction(
        db, run=run, external_state=external_state, call_context=call_context or CALL_CONTEXT,
        environment_overrides=env_overrides,
    )
    rep = orch.reproduction
    rep.record_activity(db, session=session, relative_ms=100)
    call = rep.bind_call(db, session=session, relative_ms=300)
    call, _ = rep.end_call(
        db, session=session, call_id=call.id, relative_ms=2500,
        signal=QuickAnalysisInput(verdict, findings=tuple(findings)),
    )
    assessment = orch.attach_result(
        db, run=run, session_id=session.id, call_id=call.id, external_state=external_state,
        call_context=call_context or CALL_CONTEXT, environment_overrides=env_overrides,
    )
    return run, session, call, assessment


def power_env(power):
    return {"power_supply_id": power, "phone_id": "PHONE-A", "line_id": "LINE-A"}


def fix_env(serial):
    return {
        "device":{"serial":serial}, "software":{"version":"V1"},
        "voice":{"voice_vlan_id":"100","gateway_ip":"192.0.2.1","fxs_port":"1"},
        "call":{"codec":"PCMA/8000","called_number":"10086"},
    }


def verification_call(db, rep, case, *, suffix):
    session = rep.create_session(db, case_id=case.id, profile_id="AUDIO_NOISE")
    rep.start(db, session=session)
    rep.record_activity(db, session=session, relative_ms=100)
    call = rep.bind_call(db, session=session, relative_ms=300)
    call, _ = rep.end_call(db, session=session, call_id=call.id, relative_ms=2500,
        signal=QuickAnalysisInput(CallVerdict.NO_MATCH, findings=("ACTIVE_MEDIA_WINDOW",)))
    if session.state in {"WATCHING","ACTIVITY_DETECTED"}:
        session.terminal_reason=f"FIX_VERIFY_{suffix}"
        rep.cleanup(db, session=session)
    return session, call


def main():
    eng=create_engine('sqlite+pysqlite:///:memory:'); Base.metadata.create_all(eng)
    results=[]
    with tempfile.TemporaryDirectory(prefix='voip-c3-e2e-') as td:
        base=Path(td)
        with Session(eng) as db:
            # 1) Deterministic A-B-A causal confirmation.
            case, dev, hyp = setup_case(db,'C3-E2E-ABA','LOCAL_CAPTURE_PERIODIC_INTERFERENCE')
            graph=DiagnosticQuestionGraph(DiagnosticQuestionRegistry(ROOT/'profiles'/'questions'))
            q=graph.ensure_question(db,case_id=case.id,question_key='AUDIO_NOISE_ROOT_CAUSE',state=DiagnosticQuestionState.IN_PROGRESS)
            orch=make_experiment_orch(base,'aba')
            exp=orch.create_experiment(db,case_id=case.id,profile_id='POWER_SUPPLY_AB',hypothesis_id=hyp.id,question_id=q.id)
            a1,base_session,base_call,_=run_variant(db,orch,exp,external_state=power_env('BAD'),verdict=CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'))
            _,_,_,ab=run_variant(db,orch,exp,external_state=power_env('GOOD'),verdict=CallVerdict.NO_MATCH,findings=('ACTIVE_MEDIA_WINDOW',))
            assert ab.state==CausalConclusionState.STRONGLY_SUPPORTED.value and case.status==CaseStatus.ANALYZING.value
            _,_,_,aba=run_variant(db,orch,exp,external_state=power_env('BAD'),verdict=CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'))
            assert aba.state==CausalConclusionState.ROOT_CAUSE_CONFIRMED.value and case.status==CaseStatus.ROOT_CAUSE_CONFIRMED.value
            results.append({'scenario':'POWER_SUPPLY_ABA_CAUSAL_CONFIRM','status':'PASS','causal_state':aba.state})

            # 2) Hard drift invalidates B, then a clean retry supersedes that attempt.
            case2,_,hyp2=setup_case(db,'C3-E2E-DRIFT','LOCAL_CAPTURE_PERIODIC_INTERFERENCE')
            q2=graph.ensure_question(db,case_id=case2.id,question_key='AUDIO_NOISE_ROOT_CAUSE',state=DiagnosticQuestionState.IN_PROGRESS)
            orch2=make_experiment_orch(base,'drift')
            exp2=orch2.create_experiment(db,case_id=case2.id,profile_id='POWER_SUPPLY_AB',hypothesis_id=hyp2.id,question_id=q2.id)
            run_variant(db,orch2,exp2,external_state=power_env('BAD'),verdict=CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'))
            bad,_,_,bad_assessment=run_variant(db,orch2,exp2,external_state=power_env('GOOD'),verdict=CallVerdict.NO_MATCH,findings=('ACTIVE_MEDIA_WINDOW',),env_overrides={'software':{'version':'DRIFT'}})
            assert bad.status==ExperimentRunStatus.INVALID.value and bad_assessment.state==CausalConclusionState.NOT_COMPARABLE.value
            retry,_,_,retry_assessment=run_variant(db,orch2,exp2,external_state=power_env('GOOD'),verdict=CallVerdict.NO_MATCH,findings=('ACTIVE_MEDIA_WINDOW',))
            assert retry.variant=='B' and retry_assessment.state==CausalConclusionState.STRONGLY_SUPPORTED.value
            results.append({'scenario':'HARD_DRIFT_RETRY_RECOVERY','status':'PASS','retry_run_no':retry.run_no,'causal_state':retry_assessment.state})

            # 3) Repeat-match experiment: reboot stays external; two post-reboot first-call reproductions confirm.
            case3,dev3,hyp3=setup_case(db,'C3-E2E-REBOOT','DTMF_DIGIT_ASSEMBLY_MISMATCH')
            q3=graph.ensure_question(db,case_id=case3.id,question_key='DTMF_ROOT_CAUSE',state=DiagnosticQuestionState.IN_PROGRESS)
            orch3=make_experiment_orch(base,'reboot')
            exp3=orch3.create_experiment(db,case_id=case3.id,profile_id='POST_REBOOT_FIRST_CALL',hypothesis_id=hyp3.id,question_id=q3.id)
            external={'phone_id':'PHONE-A','line_id':'LINE-A'}
            run_variant(db,orch3,exp3,external_state=external,verdict=CallVerdict.NO_MATCH,findings=())
            dev3.device_info={**dev3.device_info,'boot_id':'boot-2','uptime_seconds':5}
            run_variant(db,orch3,exp3,external_state=external,verdict=CallVerdict.MATCH,findings=('DTMF_PATH',))
            dev3.device_info={**dev3.device_info,'boot_id':'boot-3','uptime_seconds':4}
            _,_,_,repeat_assessment=run_variant(db,orch3,exp3,external_state=external,verdict=CallVerdict.MATCH,findings=('DTMF_PATH',))
            assert repeat_assessment.state==CausalConclusionState.ROOT_CAUSE_CONFIRMED.value
            results.append({'scenario':'POST_REBOOT_REPEAT_MATCH','status':'PASS','causal_state':repeat_assessment.state,'real_reboot_command_executed':False})

            # 4) Fix verification: two clean calls required before RESOLVED.
            fix_service=FixVerificationService(storage=FilesystemObjectStorage(base/'fix-objects'))
            fix=fix_service.create_fix_action(db,case_id=case.id,action_type=FixActionType.POWER_SUPPLY_REPLACE,description='replace power supply',experiment_id=exp.id)
            fv=fix_service.create_verification(db,fix_action_id=fix.id,baseline_session_id=base_session.id,baseline_call_id=base_call.id,target_finding='PERIODIC_INTERFERENCE',required_calls=2,max_calls=3)
            rep=make_reproduction(base,'fix')
            env=fix_env(dev.sn)
            s1,c1=verification_call(db,rep,case,suffix='1')
            fix_service.evaluate(db,verification=fv,verification_session_id=s1.id,verification_call_id=c1.id,baseline_environment=env,verification_environment=env,business_checks={'sip_call_established':True,'rtp_bidirectional':True})
            assert fv.status==FixVerificationStatus.RUNNING.value and case.status==CaseStatus.RESOLVING.value
            s2,c2=verification_call(db,rep,case,suffix='2')
            fix_service.evaluate(db,verification=fv,verification_session_id=s2.id,verification_call_id=c2.id,baseline_environment=env,verification_environment=env,business_checks={'sip_call_established':True,'rtp_bidirectional':True})
            evidence=db.get(Evidence,fv.evidence_id)
            assert fv.status==FixVerificationStatus.FIX_VERIFIED.value and case.status==CaseStatus.RESOLVED.value and evidence and evidence.type=='FIX_COMPARISON'
            results.append({'scenario':'FIX_VERIFICATION_TWO_CALLS','status':'PASS','fix_status':fv.status,'case_status':case.status})

    payload={'status':'PASS','passed':len(results),'total':len(results),'scenarios':results}
    (ROOT/'.reproduction-c3-e2e.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False))


if __name__=='__main__':
    main()

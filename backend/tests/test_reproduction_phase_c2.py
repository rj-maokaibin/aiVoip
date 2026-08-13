from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import CallVerdict, EvidenceKind, EvidenceSufficiency
from app.db.base import Base
from app.db.models import (
    AnalyzerRun, Case, CaseDevice, Evidence, EvidenceFinalizeRun,
    ReproductionCaptureSegment, ReproductionCaptureState,
)
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import QuickAnalysisInput


def _engine():
    eng=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _setup(db:Session, tmp_path:Path, profile_id:str):
    case=Case(case_no=f'C2-{profile_id}',summary='phase c2 evidence pipeline',status='ANALYZING'); db.add(case); db.flush()
    dev=CaseDevice(case_id=case.id,ip='198.51.100.10',ssh_port=22,sn=f'SN-{profile_id}',username='admin',device_info={}); db.add(dev); db.flush()
    root=Path(__file__).resolve().parents[2]/'profiles'
    pipe=ReproductionCapturePipeline(root=tmp_path/'capture',storage=FilesystemObjectStorage(tmp_path/'objects'))
    orch=ReproductionOrchestrator(registry=ReproductionProfileRegistry(root),platform=MockReproductionPlatform(),capture_pipeline=pipe)
    session=orch.create_session(db,case_id=case.id,profile_id=profile_id); orch.start(db,session=session)
    return case,dev,orch,session,pipe


def _one_call(db,orch,session,*,findings,verdict=CallVerdict.MATCH,end_ms=2500):
    orch.record_activity(db,session=session,relative_ms=100)
    call=orch.bind_call(db,session=session,relative_ms=300)
    return orch.end_call(db,session=session,call_id=call.id,relative_ms=end_ms,signal=QuickAnalysisInput(verdict,findings=tuple(findings)))


def test_file_backed_capture_freeze_persists_immutable_raw_segments_and_lineage(tmp_path):
    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,_=_setup(db,tmp_path,'AUDIO_NOISE')
        call,decision=_one_call(db,orch,session,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'))
        assert decision.status==EvidenceSufficiency.SUFFICIENT.value
        segs=list(db.scalars(select(ReproductionCaptureSegment).where(ReproductionCaptureSegment.session_id==session.id)))
        assert len(segs)>=6 and all(Path(x.local_path).exists() for x in segs)
        retained=[x for x in segs if x.retained]
        assert retained and all(x.evidence_id for x in retained)
        for seg in retained:
            ev=db.get(Evidence,seg.evidence_id)
            assert ev.kind==EvidenceKind.RAW.value
            assert ev.sha256==seg.sha256
        call_evidence=db.get(Evidence,call.quick_analysis_json['input_evidence_ids'][0])
        assert call_evidence.type=='CALL_PCAP' and call_evidence.kind==EvidenceKind.DERIVED.value


def test_live_and_call_quick_analyzers_consume_real_mock_pcap_evidence(tmp_path):
    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,_=_setup(db,tmp_path,'AUDIO_NOISE')
        orch.record_activity(db,session=session,relative_ms=100)
        call=orch.bind_call(db,session=session,relative_ms=300)
        assert call.live_summary_json['mode']=='LIVE'
        assert {'SIP_CALL_LIVE','RTP_BASIC_LIVE','PCM_STREAM_HEALTH'} <= set(call.live_summary_json['findings'])
        call,decision=orch.end_call(db,session=session,call_id=call.id,relative_ms=2600,
            signal=QuickAnalysisInput(CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION')))
        assert call.verdict=='MATCH'
        assert {'ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'} <= set(call.quick_analysis_json['findings'])
        run=db.get(AnalyzerRun,call.quick_analysis_json['analyzer_run_id'])
        assert run.input_evidence_ids==call.quick_analysis_json['input_evidence_ids']
        assert run.output_evidence_ids==call.quick_analysis_json['output_evidence_ids']
        assert run.config_snapshot['mode']=='CALL_QUICK'


@pytest.mark.parametrize(('profile_id','signal_findings','expected_finding'),[
    ('AUDIO_STUTTER',('ACTIVE_MEDIA_WINDOW','RTP_BURST_LOSS'),'RTP_BURST_LOSS'),
    ('ONE_WAY_AUDIO',('CALL_MEDIA_DIRECTION','ONE_WAY_RTP_MEDIA'),'ONE_WAY_RTP_MEDIA'),
    ('ECHO',('ECHO_PATH',),'ECHO_PATH'),
    ('DTMF_LOSS',('DTMF_PATH',),'DTMF_PATH'),
])
def test_existing_semantic_analyzers_detect_mock_target_from_captured_packets(tmp_path,profile_id,signal_findings,expected_finding):
    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,_=_setup(db,tmp_path,profile_id)
        call,decision=_one_call(db,orch,session,findings=signal_findings)
        assert call.verdict=='MATCH'
        assert expected_finding in call.quick_analysis_json['findings']
        assert decision.sufficient is True
        assert session.state=='COMPLETED'


def test_session_finalization_writes_manifest_and_is_idempotent(tmp_path):
    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'AUDIO_NOISE')
        _one_call(db,orch,session,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'))
        runs=list(db.scalars(select(EvidenceFinalizeRun).where(EvidenceFinalizeRun.session_id==session.id)))
        assert len(runs)==1 and runs[0].status=='SUCCESS' and runs[0].manifest_sha256
        again=pipe.finalize_session(db,session=session)
        assert again['idempotent'] is True
        assert len(list(db.scalars(select(EvidenceFinalizeRun).where(EvidenceFinalizeRun.session_id==session.id))))==1
        bundle=build_reproduction_evidence_bundle(db,session)
        assert bundle['schema_version']==2
        assert bundle['capture_pipeline']['state']['finalized'] is True
        assert bundle['capture_pipeline']['finalizations'][0]['status']=='SUCCESS'
        manifest_path=(tmp_path/'objects'/runs[0].manifest_object_key)
        assert manifest_path.exists() and manifest_path.stat().st_size>0


def test_ring_eviction_deletes_unfrozen_local_segment_but_never_retained_evidence(tmp_path):
    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'AUDIO_NOISE')
        st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        st.pretrigger_ms=1000
        a=pipe.append_log(db,session=session,start_ms=0,end_ms=100,data=b'old')
        b=pipe.append_log(db,session=session,start_ms=1900,end_ms=2000,data=b'new')
        assert Path(a.local_path).exists() and Path(b.local_path).exists()
        evicted=pipe.evict_ring(db,session=session,current_end_ms=2500)
        assert a.id in evicted and not Path(a.local_path).exists()
        assert Path(b.local_path).exists() and b.status=='ACTIVE'

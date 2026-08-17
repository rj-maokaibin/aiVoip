from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    AttemptStatus, CallVerdict, CaptureChannel, CaptureSegmentStatus, EvidenceKind,
    EvidenceSufficiency, ReproductionCallStatus, RetentionClass,
)
from app.db.base import Base
from app.db.models import (
    AnalyzerRun, Case, CaseDevice, Evidence, EvidenceFinalizeRun,
    ReproductionAttempt, ReproductionCall, ReproductionCaptureSegment,
    ReproductionCaptureState,
)
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.capture_pipeline import ReproductionCapturePipeline, _utcnow
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.pcap_codec import PcapRecord, build_pcap, udp_ethernet_frame
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


def test_build_call_capture_includes_post_onhook_media_tail(tmp_path):
    """Per-call capture window must extend past the FXS on-hook anchor by the
    profile post-capture window, so in-call DTMF/audio pressed just before
    hang-up (landed in the following ring segment) is not truncated from the
    per-call analysis. Regression for real session e694d134 where the DTMF tail
    (4567890) was cut off because the next segment started after the on-hook
    anchor."""
    def pcap_stats(path):
        data=Path(path).read_bytes(); magic=struct.unpack('<I',data[:4])[0]
        pos=24; n=0; first=None; last=None
        while pos+16<=len(data):
            sec,frac,incl,_=struct.unpack('<IIII',data[pos:pos+16])
            ts=sec+frac*1e-6
            if first is None: first=ts
            last=ts
            pos+=16+incl; n+=1
        return n,first,last

    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'DTMF_LOSS')
        attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=1,
            status=AttemptStatus.COMPLETED.value,valid=True,start_anchor_type='FXS_OFFHOOK',start_anchor_ms=1000,
            end_anchor_type='FXS_ONHOOK',end_anchor_ms=5000)
        db.add(attempt); db.flush()
        call=ReproductionCall(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,call_no=1,
            status=ReproductionCallStatus.ACTIVE.value,started_at=_utcnow())
        db.add(call); db.flush()

        def add_seg(no,start,end,records):
            p=tmp_path/f'cap_{no}.pcap'; data=build_pcap(records); p.write_bytes(data)
            db.add(ReproductionCaptureSegment(session_id=session.id,attempt_id=attempt.id,call_id=call.id,
                channel=CaptureChannel.PCAP.value,segment_no=no,start_ms=start,end_ms=end,local_path=str(p),
                content_type='application/vnd.tcpdump.pcap',size_bytes=p.stat().st_size,sha256=hashlib.sha256(data).hexdigest(),
                status=CaptureSegmentStatus.FROZEN.value,frozen=True,retained=True,
                retention_class=RetentionClass.PERMANENT_RAW.value))
            db.flush()
        add_seg(1,800,5200,[PcapRecord(1.0,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'aaa'))])
        # Media tail: this segment starts AFTER the on-hook anchor (5200 > 5000).
        # With the fix the window extends to 5000+post_capture(3000)=8000, so the
        # tail is included instead of being truncated from the per-call analysis.
        add_seg(2,5200,9000,[PcapRecord(6.0,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'bbb'))])

        pcap_path,ev=pipe.build_call_capture(db,session=session,call=call)
        n,first,last=pcap_stats(pcap_path)
        assert n==2, f'expected in-window + post-onhook tail merged, got {n} packets'
        assert abs(first-1.0)<0.01 and abs(last-6.0)<0.01
        assert ev.type=='CALL_PCAP' and ev.size_bytes>0


def test_build_call_capture_does_not_bleed_into_next_attempt(tmp_path):
    """The post-capture window extension must stop at the next attempt's start
    so a later call's media is never merged into the current call's analysis."""
    def pcap_stats(path):
        data=Path(path).read_bytes(); pos=24; n=0; first=None; last=None
        while pos+16<=len(data):
            sec,frac,incl,_=struct.unpack('<IIII',data[pos:pos+16])
            ts=sec+frac*1e-6
            if first is None: first=ts
            last=ts
            pos+=16+incl; n+=1
        return n,first,last

    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'DTMF_LOSS')
        attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=1,
            status=AttemptStatus.COMPLETED.value,valid=True,start_anchor_type='FXS_OFFHOOK',start_anchor_ms=1000,
            end_anchor_type='FXS_ONHOOK',end_anchor_ms=5000)
        db.add(attempt); db.flush()
        # a later attempt (next call) starts at 7000ms ¡ª the extension (5000+3000)
        # must stop there and not pull in the next call's segment [7000,12000].
        attempt2=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=2,
            status=AttemptStatus.COMPLETED.value,valid=True,start_anchor_type='FXS_OFFHOOK',start_anchor_ms=7000,
            end_anchor_type='FXS_ONHOOK',end_anchor_ms=11000)
        db.add(attempt2); db.flush()
        call=ReproductionCall(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,call_no=1,
            status=ReproductionCallStatus.ACTIVE.value,started_at=_utcnow())
        db.add(call); db.flush()

        def add_seg(no,start,end,records,call_id=None):
            p=tmp_path/f'cap2_{no}.pcap'; data=build_pcap(records); p.write_bytes(data)
            db.add(ReproductionCaptureSegment(session_id=session.id,attempt_id=attempt.id,call_id=call_id or call.id,
                channel=CaptureChannel.PCAP.value,segment_no=no,start_ms=start,end_ms=end,local_path=str(p),
                content_type='application/vnd.tcpdump.pcap',size_bytes=p.stat().st_size,sha256=hashlib.sha256(data).hexdigest(),
                status=CaptureSegmentStatus.FROZEN.value,frozen=True,retained=True,
                retention_class=RetentionClass.PERMANENT_RAW.value))
            db.flush()
        add_seg(1,800,5200,[PcapRecord(1.0,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'aaa'))])
        # next call's segment bound to a DIFFERENT call -> must NOT be merged in.
        add_seg(2,7000,12000,[PcapRecord(9.0,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'xxx'))],call_id='other-call')

        pcap_path,ev=pipe.build_call_capture(db,session=session,call=call)
        n,first,last=pcap_stats(pcap_path)
        assert n==1, f'expected only current-call media, got {n} packets (bleed into next attempt)'
        assert abs(first-1.0)<0.01
        assert ev.type=='CALL_PCAP'


def test_build_call_capture_includes_call_bound_tail_beyond_post_capture(tmp_path):
    """Per-call window must extend to cover retained segments that are explicitly
    bound to THIS call even when they start after on-hook + post_capture. The
    fixed post-capture window alone is insufficient because the media stream runs
    to the SIP BYE, so the final key press / audio tail can land in a ring segment
    starting beyond on-hook+3s. Regression for real session f43f6b7d where the
    last DTMF '0' was lost (seg5 started 50454 > on-hook 44600 + 3s = 47600)."""
    def pcap_stats(path):
        data=Path(path).read_bytes(); pos=24; n=0; first=None; last=None
        while pos+16<=len(data):
            sec,frac,incl,_=struct.unpack('<IIII',data[pos:pos+16])
            ts=sec+frac*1e-6
            if first is None: first=ts
            last=ts
            pos+=16+incl; n+=1
        return n,first,last

    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'DTMF_LOSS')
        attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=1,
            status=AttemptStatus.COMPLETED.value,valid=True,start_anchor_type='FXS_OFFHOOK',start_anchor_ms=1000,
            end_anchor_type='FXS_ONHOOK',end_anchor_ms=5000)
        db.add(attempt); db.flush()
        call=ReproductionCall(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,call_no=1,
            status=ReproductionCallStatus.ACTIVE.value,started_at=_utcnow())
        db.add(call); db.flush()

        def add_seg(no,start,end,records):
            p=tmp_path/f'cap3_{no}.pcap'; data=build_pcap(records); p.write_bytes(data)
            db.add(ReproductionCaptureSegment(session_id=session.id,attempt_id=attempt.id,call_id=call.id,
                channel=CaptureChannel.PCAP.value,segment_no=no,start_ms=start,end_ms=end,local_path=str(p),
                content_type='application/vnd.tcpdump.pcap',size_bytes=p.stat().st_size,sha256=hashlib.sha256(data).hexdigest(),
                status=CaptureSegmentStatus.FROZEN.value,frozen=True,retained=True,
                retention_class=RetentionClass.PERMANENT_RAW.value))
            db.flush()
        add_seg(1,800,5200,[PcapRecord(1.0,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'aaa'))])
        # Media tail bound to THIS call, starting AFTER on-hook(5000)+post_capture(3000)=8000.
        # Without the call-bound extension this segment is dropped -> tail DTMF lost.
        add_seg(2,8500,11000,[PcapRecord(9.5,udp_ethernet_frame('192.0.2.1','192.0.2.2',40000,41000,b'bbb'))])

        pcap_path,ev=pipe.build_call_capture(db,session=session,call=call)
        n,first,last=pcap_stats(pcap_path)
        assert n==2, f'expected in-window + call-bound tail beyond post_capture, got {n} packets'
        assert abs(first-1.0)<0.01 and abs(last-9.5)<0.01
        assert ev.type=='CALL_PCAP'


def test_reconcile_pcm_dtmf_records_authoritative_sequences(tmp_path):
    """After CALL_QUICK, PCM-media DTMF sequences (media truth that survives fast
    key presses the FXS event report drops) are reconciled into the event record
    as authoritative PCM_DTMF_SEQUENCE events, idempotently."""
    from app.contracts.enums import CallRole
    from app.db.models import ReproductionEventRecord
    from app.reproduction.quick import QuickAnalysisResult

    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'DTMF_LOSS')
        attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=1,
            status=AttemptStatus.COMPLETED.value,valid=True,start_anchor_type='FXS_OFFHOOK',start_anchor_ms=1000,
            end_anchor_type='FXS_ONHOOK',end_anchor_ms=5000)
        db.add(attempt); db.flush()
        call=ReproductionCall(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,call_no=1,
            status=ReproductionCallStatus.ACTIVE.value,started_at=_utcnow())
        db.add(call); db.flush()

        result=QuickAnalysisResult(verdict=CallVerdict.NO_MATCH,role=CallRole.CONTROL,findings=(),
            hard_contradiction=False,capture_recovery_required=False,external_action_required=False,
            metrics={},analyzer_run_id='run1',
            pcm_dtmf_sequences=({'digits':'11110000','event_count':8,'min_confidence':1.0,
                                 'tap':'pcm_rx','session_index':0,'start_seconds':9.0},))
        orch._reconcile_pcm_dtmf_events(db,session=session,call=call,result=result)
        db.flush()
        events=list(db.scalars(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id==session.id,
            ReproductionEventRecord.event_type=='PCM_DTMF_SEQUENCE')))
        assert len(events)==1
        assert events[0].payload_json['digits']=='11110000'
        assert events[0].source=='PCM_MEDIA_ANALYSIS' and events[0].call_id==call.id
        assert events[0].payload_json.get('supplementary') is True
        # idempotent: re-running does not duplicate the sequence event
        orch._reconcile_pcm_dtmf_events(db,session=session,call=call,result=result)
        db.flush()
        again=list(db.scalars(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id==session.id,
            ReproductionEventRecord.event_type=='PCM_DTMF_SEQUENCE')))
        assert len(again)==1


def test_bind_call_backfills_fxs_dtmf_call_id(tmp_path):
    """FXS DTMF events recorded BEFORE the Call row exists (Call binding trails
    the physical answer by ~1 segment/~8s) must be backfilled with the new call_id
    once the Call is bound. Regression for real session d60b2f5b (RP-D08): dial and
    in-call digits (301123#789) were fully captured in PCM but their FXS event
    call_id stayed NULL, making in-call DTMF unobservable at the event layer."""
    from app.db.models import ReproductionEventRecord
    from app.reproduction.profile import ReproductionProfileRegistry
    from app.reproduction.quick import QuickAnalysisInput

    eng=_engine()
    with Session(eng) as db:
        _,_,orch,session,pipe=_setup(db,tmp_path,'AUDIO_NOISE')
        orch.record_activity(db,session=session,relative_ms=100)
        attempt=db.scalar(select(ReproductionAttempt).where(
            ReproductionAttempt.session_id==session.id,ReproductionAttempt.status=='ACTIVE'))
        assert attempt is not None
        # Dial DTMF recorded before any Call exists -> call_id must be NULL.
        class _Ev:
            timestamp='2026-08-17 04:38:01.461000'
            event='DTMF'
            digit='3'
        class _Ev2:
            timestamp='2026-08-17 04:38:01.461000'
            event='DTMF'
            digit='0'
        orch.record_fxs_event(db,session=session,event=_Ev(),actor='reproduction-worker')
        orch.record_fxs_event(db,session=session,event=_Ev2(),actor='reproduction-worker')
        db.flush()
        pre=list(db.scalars(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id==session.id,
            ReproductionEventRecord.event_type=='FXS_DTMF')))
        assert len(pre)==2 and all(e.call_id is None for e in pre)
        # Bind the call -> backfill must attribute both DTMF events to the call.
        call=orch.bind_call(db,session=session,relative_ms=300,
                            binding_event='SIP_INVITE',actor='reproduction-worker')
        db.flush()
        post=list(db.scalars(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id==session.id,
            ReproductionEventRecord.event_type=='FXS_DTMF')))
        assert len(post)==2 and all(e.call_id==call.id for e in post)
        assert all((e.payload_json or {}).get('in_call') is True for e in post)

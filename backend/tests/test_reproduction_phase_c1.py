from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    CallRole, CallVerdict, CaptureStage, CleanupStatus, EvidenceSufficiency,
    ReproductionState,
)
from app.core.errors import AppError
from app.db.base import Base
from app.db.models import (
    Case, CaseDevice, DeviceDiagnosticLock, ReproductionAttempt, ReproductionCall,
    ReproductionSession,
)
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import QuickAnalysisInput
from app.reproduction.recovery import RecoveryReconciler
from app.reproduction.ring import RingSegment, SegmentedRingBuffer


def _engine():
    eng=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case_device(db:Session, no='RC-1', *, device_info=None):
    case=Case(case_no=no,summary='reproduction mock',status='ANALYZING')
    db.add(case); db.flush()
    device=CaseDevice(case_id=case.id,ip='198.51.100.10',ssh_port=22,sn=f'SN-{no}',username='admin',device_info=device_info or {})
    db.add(device); db.flush()
    return case,device


_MOCK_CAPTURE_TMP=tempfile.TemporaryDirectory(prefix='voip-c1-tests-')

def _orch():
    root=Path(__file__).resolve().parents[2]/'profiles'
    base=Path(_MOCK_CAPTURE_TMP.name)/uuid4().hex
    pipe=ReproductionCapturePipeline(root=base/'capture',storage=FilesystemObjectStorage(base/'objects'))
    return ReproductionOrchestrator(registry=ReproductionProfileRegistry(root),platform=MockReproductionPlatform(),capture_pipeline=pipe)


def test_all_eight_frozen_reproduction_profiles_load_and_generic_exists():
    root=Path(__file__).resolve().parents[2]/'profiles'
    registry=ReproductionProfileRegistry(root)
    ids=[x.definition.id for x in registry.list()]
    assert ids==sorted(['REGISTER_FAILURE','CALL_SETUP_FAILURE','ONE_WAY_AUDIO','AUDIO_STUTTER','AUDIO_NOISE','DTMF_LOSS','ECHO','VOIP_GENERIC_FULL_CAPTURE'])
    assert registry.get('VOIP_GENERIC_FULL_CAPTURE').definition.allow_partial_capability_downgrade is True


def test_autonomous_arm_resolves_context_and_reaches_watching_only_after_data_plane_ready():
    eng=_engine()
    with Session(eng) as db:
        case,device=_case_device(db)
        orch=_orch()
        session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE')
        orch.start(db,session=session,owner_worker='w1')
        assert session.state==ReproductionState.WATCHING.value
        assert session.voice_runtime_context_json['voice_interface']=='br-lan_100'
        assert session.voice_runtime_context_json['voice_gateway_ip']=='192.0.2.1'
        assert session.capture_completeness=='COMPLETE'
        assert session.cleanup_required is True
        assert db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id==session.id)) is not None


def test_arm_failure_never_enters_watching_and_cleanup_is_verified():
    eng=_engine()
    with Session(eng) as db:
        case,device=_case_device(db,device_info={'mock_capture':{'pcm_tx_fail':True}})
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE')
        orch.start(db,session=session,owner_worker='w1')
        assert session.state==ReproductionState.PARTIAL_SUCCESS.value
        assert session.terminal_reason=='ARM_FAILED'
        assert session.cleanup_status==CleanupStatus.CLEANUP_VERIFIED.value
        assert session.capture_completeness=='UNAVAILABLE'


def test_same_dut_allows_only_one_active_reproduction_owner():
    eng=_engine()
    with Session(eng) as db:
        case,device=_case_device(db)
        orch=_orch()
        a=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE',device_id=device.id)
        b=orch.create_session(db,case_id=case.id,profile_id='DTMF_LOSS',device_id=device.id)
        orch.start(db,session=a,owner_worker='w1')
        orch.start(db,session=b,owner_worker='w2')
        assert a.state==ReproductionState.WATCHING.value
        assert b.state==ReproductionState.WAITING_DEVICE_RESOURCE.value
        # A completes cleanup; B may then acquire and arm.
        orch.cancel(db,session=a,actor='tester')
        assert a.state==ReproductionState.CANCELLED.value
        orch.start(db,session=b,owner_worker='w2')
        assert b.state==ReproductionState.WATCHING.value


def test_invalid_attempt_returns_to_watching_without_ending_session():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_STUTTER'); orch.start(db,session=session)
        attempt=orch.record_activity(db,session=session,relative_ms=100)
        orch.end_activity_without_call(db,session=session,attempt_id=attempt.id,relative_ms=900)
        assert attempt.status=='INVALID'
        assert session.state==ReproductionState.WATCHING.value


def test_call_can_bind_when_low_level_anchor_was_missed_and_becomes_control():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        call=orch.bind_call(db,session=session,relative_ms=500,external_call_ref='call-1')
        call,decision=orch.end_call(db,session=session,call_id=call.id,relative_ms=5000,
                                   signal=QuickAnalysisInput(CallVerdict.NO_MATCH,findings=('ACTIVE_MEDIA_WINDOW',)))
        attempt=db.get(ReproductionAttempt,call.attempt_id)
        assert attempt.details_json['low_level_anchor_missed'] is True
        assert call.role==CallRole.CONTROL.value
        assert decision.status==EvidenceSufficiency.INSUFFICIENT_RETRY.value
        assert session.state==ReproductionState.WATCHING.value


def test_target_with_required_findings_completes_session_and_cleanup():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        orch.record_activity(db,session=session,relative_ms=100)
        call=orch.bind_call(db,session=session,relative_ms=500)
        call,decision=orch.end_call(db,session=session,call_id=call.id,relative_ms=5000,
                                   signal=QuickAnalysisInput(CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION')))
        assert decision.sufficient is True
        assert call.role==CallRole.TARGET.value
        assert session.primary_target_call_id==call.id
        assert session.state==ReproductionState.COMPLETED.value
        assert session.cleanup_status==CleanupStatus.CLEANUP_VERIFIED.value
        bundle=build_reproduction_evidence_bundle(db,session)
        assert bundle['session']['primary_target_call_id']==call.id
        assert bundle['calls'][0]['verdict']=='MATCH'


def test_missing_finding_automatically_enhances_between_attempts_not_mid_call():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        orch.record_activity(db,session=session,relative_ms=100)
        call=orch.bind_call(db,session=session,relative_ms=300)
        _,decision=orch.end_call(db,session=session,call_id=call.id,relative_ms=2000,
                                signal=QuickAnalysisInput(CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW',)))
        assert decision.status==EvidenceSufficiency.INSUFFICIENT_ENHANCE.value
        assert session.capture_stage==CaptureStage.ENHANCED.value
        assert session.state==ReproductionState.WATCHING.value


def test_generic_profile_can_arm_partial_when_pcm_is_unavailable_but_never_fakes_zero():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db,device_info={'mock_capture':{'pcm_rx_fail':True,'pcm_tx_fail':True}})
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='VOIP_GENERIC_FULL_CAPTURE'); orch.start(db,session=session)
        assert session.state==ReproductionState.WATCHING.value
        assert session.capture_completeness=='PARTIAL'
        bundle=build_reproduction_evidence_bundle(db,session)
        assert bundle['capture_health']['PCM_RX']['status']=='UNAVAILABLE'
        assert bundle['capture_health']['PCM_RX']['packet_count']==0


def test_cleanup_reverse_validation_failure_keeps_lock_for_watchdog():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db,device_info={'mock_cleanup':{'pcm_tx_leak':True}})
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        orch.cancel(db,session=session)
        assert session.state==ReproductionState.CLEANUP_FAILED.value
        assert session.cleanup_status==CleanupStatus.CLEANUP_FAILED.value
        lock=db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id==session.id))
        assert lock is not None and lock.status=='ACTIVE'


def test_lease_expiry_reconciler_orphans_then_cleans_up():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_STUTTER'); orch.start(db,session=session,owner_worker='dead-worker')
        lock=db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id==session.id))
        lock.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        recovered=RecoveryReconciler(orch).reconcile_expired_leases(db)
        assert recovered==[session.id]
        assert session.cleanup_status==CleanupStatus.CLEANUP_VERIFIED.value
        assert session.state==ReproductionState.PARTIAL_SUCCESS.value


def test_segmented_ring_evicts_before_trigger_and_preserves_after_freeze():
    ring=SegmentedRingBuffer(pretrigger_seconds=10,segment_seconds=5)
    ring.append(RingSegment('s1',0,5000,'s1.pcap'))
    ring.append(RingSegment('s2',5000,10000,'s2.pcap'))
    evicted=ring.append(RingSegment('s3',15000,20000,'s3.pcap'))
    assert [x.segment_id for x in evicted]==['s1']
    frozen=ring.freeze(18000)
    assert {x.segment_id for x in frozen}=={'s2','s3'}
    evicted2=ring.append(RingSegment('s4',20000,25000,'s4.pcap'))
    assert evicted2==[] and ring.preserve_mode is True


def test_external_action_path_cleans_up_releases_lock_then_rearms_after_completion():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        orch.record_activity(db,session=session,relative_ms=100)
        call=orch.bind_call(db,session=session,relative_ms=300)
        _,decision=orch.end_call(
            db,session=session,call_id=call.id,relative_ms=2000,
            signal=QuickAnalysisInput(CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW',),external_action_required=True),
        )
        assert decision.status==EvidenceSufficiency.INSUFFICIENT_EXTERNAL_ACTION.value
        assert session.state==ReproductionState.WAITING_EXTERNAL_ACTION.value
        lock=db.scalar(select(DeviceDiagnosticLock).where(DeviceDiagnosticLock.session_id==session.id))
        assert lock is not None and lock.status=='RELEASED'
        orch.start(db,session=session,owner_worker='w2',actor='tester')
        assert session.state==ReproductionState.WATCHING.value


def test_case_level_retry_creates_new_bounded_session_after_watch_timeout_cleanup():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_STUTTER'); orch.start(db,session=session)
        orch.watch_timeout(db,session=session)
        assert session.state==ReproductionState.PARTIAL_SUCCESS.value
        retry=orch.create_case_retry(db,session=session)
        assert retry is not None and retry.retry_parent_session_id==session.id and retry.state==ReproductionState.CREATED.value
        # profile max_sessions=2 -> no third session after second reaches a retryable terminal reason
        retry.terminal_reason='WATCH_TIMEOUT'
        assert orch.create_case_retry(db,session=retry) is None


def test_cleanup_watchdog_can_recover_after_transient_leak_disappears():
    eng=_engine()
    with Session(eng) as db:
        case,device=_case_device(db,device_info={'mock_cleanup':{'pcm_tx_leak':True}})
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_NOISE'); orch.start(db,session=session)
        orch.cancel(db,session=session)
        assert session.state==ReproductionState.CLEANUP_FAILED.value
        device.device_info={'mock_cleanup':{}}
        db.flush()
        orch.retry_cleanup(db,session=session,actor='watchdog')
        assert session.state==ReproductionState.CANCELLED.value
        assert session.cleanup_status==CleanupStatus.CLEANUP_VERIFIED.value


def test_capture_health_monitor_detects_required_channel_degradation_without_mid_call_reconfiguration():
    eng=_engine()
    with Session(eng) as db:
        case,_=_case_device(db)
        orch=_orch(); session=orch.create_session(db,case_id=case.id,profile_id='AUDIO_STUTTER'); orch.start(db,session=session)
        orch.platform.degrade_channel(session.id, __import__('app.contracts.enums',fromlist=['CaptureChannel']).CaptureChannel.PCM_RX)
        decision=orch.poll_capture_health(db,session=session)
        assert decision.healthy is False and decision.failed_required_channels==('PCM_RX',)
        assert session.state==ReproductionState.WATCHING.value
        # Health monitor records the failure; it does not change debug/capture while a call is in progress.

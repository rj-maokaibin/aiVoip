from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

from app.contracts.enums import (
    AnchorType, ArmValidationStatus, AttemptStatus, CallRole, CallVerdict, CaptureChannel, CaptureStage,
    ChannelHealth, CleanupRunStatus, CleanupStatus, DiagnosticQuestionState, EndPolicy,
    EventType, EvidenceCompleteness, EvidenceGapAction, EvidenceSufficiency, ReproductionCallStatus,
    ReproductionEvent, ReproductionProfileStatus, ReproductionState, TimestampSource,
)
from app.core.errors import AppError
from app.db.models import (
    Case, CaseDevice, CaptureChannelHealth, CleanupRun, DiagnosticQuestion, ReproductionAttempt,
    ReproductionCall, ReproductionCaptureSegment, ReproductionEventRecord, ReproductionProfile, ReproductionProfileVersion,
    ReproductionSession, VoiceRuntimeContextSnapshot, Evidence,
)
from app.reproduction.barriers import ArmReadinessBarrier, CleanupReadinessBarrier
from app.reproduction.locks import acquire_device_lock, heartbeat_device_lock, quarantine_device_lock, release_device_lock
from app.reproduction.health import CaptureHealthMonitor
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.live import LiveReproductionAnalyzer
from app.reproduction.mock_platform import MockReproductionPlatform, VoiceRuntimeContext
from app.reproduction.pcm_cleanup import PcmCleanupChannelResult, PcmCleanupGuard
from app.reproduction.fxs_event_monitor import FxsEventMonitor
from app.reproduction.profile import LoadedReproductionProfile, ReproductionProfileDefinition, ReproductionProfileRegistry
from app.reproduction.quick import MockCallQuickAnalyzer, QuickAnalysisInput, QuickAnalysisResult
from app.reproduction.state_machine import TERMINAL_STATES, transition_session
from app.reproduction.sufficiency import EvidenceSufficiencyEvaluator, SufficiencyDecision
from app.reproduction.question_graph import DiagnosticQuestionGraph
from app.services.audit import audit
from app.services.events import emit_event


_PCM_CHANNEL_TO_STOP_ACTION = {
    'PCM_RX': 'STOP_PCM_RX',
    'PCM_TX': 'STOP_PCM_TX',
}


def _pcm_guard_channel_snapshot(result: PcmCleanupChannelResult) -> dict:
    """Translate a PCM guard result into the reverse-validation snapshot shape.

    The guard decides the observed PCM channel state on the real device, so the
    result fully replaces the channel entry that a Mock platform would produce.
    """
    return {
        'status': 'STOPPED' if result.quiet_verified else 'DEGRADED',
        'packet_count': result.packets_after,
        'advancing': not result.quiet_verified,
        'enabled': not result.quiet_verified,
        'quiet_verified': result.quiet_verified,
        'off_executed': result.off_executed,
        'retry_blocked': result.retry_blocked,
        'guard': 'PCM_GUARD',
    }


def _utcnow():
    return datetime.now(timezone.utc)


class ReproductionOrchestrator:
    """Persistent M6.2 orchestration core shared by Mock and verified EC-02 platforms.

    Device commands remain behind the platform adapter; this layer owns the
    deterministic state machine, evidence boundaries and cleanup guarantees.
    """

    def __init__(self, *, registry: ReproductionProfileRegistry | None = None, platform=None, capture_pipeline: ReproductionCapturePipeline | None = None, pcm_cleanup_guard: PcmCleanupGuard | None = None, fxs_event_monitor: FxsEventMonitor | None = None):
        self.registry=registry or ReproductionProfileRegistry()
        self.platform=platform or MockReproductionPlatform()
        self.capture= capture_pipeline or ReproductionCapturePipeline()
        self.quick_analyzer=MockCallQuickAnalyzer(storage=self.capture.storage)
        self.live_analyzer=LiveReproductionAnalyzer(storage=self.capture.storage)
        self.sufficiency=EvidenceSufficiencyEvaluator()
        self.questions=DiagnosticQuestionGraph()
        # Optional transport-injected FXS event monitor. When present, watch-time activity is
        # driven by real DUT OFFHOOK/DTMF/ONHOOK events instead of the mock platform.
        self.fxs_event_monitor=fxs_event_monitor
        self.pcm_cleanup_guard=pcm_cleanup_guard

    def _persist_profile(self, db: Session, loaded: LoadedReproductionProfile) -> None:
        d=loaded.definition
        row=db.scalar(select(ReproductionProfile).where(ReproductionProfile.profile_key==d.id))
        if not row:
            row=ReproductionProfile(profile_key=d.id,name=d.name,active_version=d.version)
            db.add(row); db.flush()
        version=db.scalar(select(ReproductionProfileVersion).where(
            ReproductionProfileVersion.profile_id==row.id,
            ReproductionProfileVersion.version==d.version,
        ))
        if not version:
            version=ReproductionProfileVersion(
                profile_id=row.id,version=d.version,checksum=loaded.checksum,
                status=ReproductionProfileStatus.ACTIVE.value,content_json=d.canonical(),
                created_by='phase-c-profile-seed',approved_by='phase-c-profile-reviewer',
                approved_at=_utcnow(),
            )
            db.add(version)
        row.name=d.name
        row.active_version=d.version
        db.flush()

    def create_session(
        self,
        db: Session,
        *,
        case_id: str,
        profile_id: str | None = None,
        symptom_class: str | None = None,
        device_id: str | None = None,
        actor: str | None = None,
        retry_parent_session_id: str | None = None,
    ) -> ReproductionSession:
        case=db.get(Case,case_id)
        if not case: raise AppError('CASE_NOT_FOUND')
        if device_id:
            device=db.get(CaseDevice,device_id)
            if not device or device.case_id!=case_id: raise AppError('VOICE_CONTEXT_NOT_FOUND',details={'device_id':device_id})
        else:
            device=db.scalar(select(CaseDevice).where(CaseDevice.case_id==case_id).order_by(CaseDevice.created_at))
            if not device: raise AppError('VOICE_CONTEXT_NOT_FOUND',details={'case_id':case_id,'reason':'NO_CASE_DEVICE'})
        try:
            loaded=self.registry.get(profile_id) if profile_id else self.registry.select_for_symptom(symptom_class)
        except Exception as exc:
            raise AppError('REPRODUCTION_PROFILE_NOT_FOUND',details={'profile_id':profile_id,'symptom_class':symptom_class}) from exc
        self._persist_profile(db,loaded)
        d=loaded.definition
        session=ReproductionSession(
            case_id=case_id,device_id=device.id,profile_key=d.id,profile_version=d.version,
            profile_checksum=loaded.checksum,effective_profile_snapshot=d.canonical(),
            platform_profile_id=self.platform.platform_id,platform_profile_version=self.platform.version,
            state=ReproductionState.CREATED.value,capture_stage=CaptureStage.BASE.value,
            cleanup_required=False,cleanup_status=CleanupStatus.NOT_REQUIRED.value,
            capture_completeness=EvidenceCompleteness.UNAVAILABLE.value,
            evidence_sufficiency=EvidenceSufficiency.NOT_EVALUATED.value,
            retry_parent_session_id=retry_parent_session_id,
        )
        db.add(session); db.flush()
        q=self.questions.ensure_question(
            db,case_id=case_id,session_id=session.id,question_key=d.sufficiency.question_key,
            state=DiagnosticQuestionState.IN_PROGRESS,selected_reason='reproduction_profile_sufficiency_question',
        )
        q.requirements_json={
            'question_template':q.requirements_json,
            'reproduction_sufficiency':d.sufficiency.model_dump(mode='json'),
        }
        db.flush()
        audit(db,case_id=case_id,actor=actor,event_type=EventType.REPRODUCTION_CREATED.value,
              action='REPRODUCTION_CREATE',target_type='reproduction_session',target_id=session.id,
              detail={'profile_id':d.id,'profile_version':d.version,'profile_checksum':loaded.checksum,'device_id':device.id})
        db.flush()
        return session

    def _profile(self, session: ReproductionSession) -> ReproductionProfileDefinition:
        # Effective snapshot is frozen at Session creation, so hot profile changes cannot affect an active Session.
        return ReproductionProfileDefinition.model_validate(session.effective_profile_snapshot)

    def _device(self, db: Session, session: ReproductionSession) -> CaseDevice:
        device=db.get(CaseDevice,session.device_id)
        if not device: raise AppError('VOICE_CONTEXT_NOT_FOUND',details={'device_id':session.device_id})
        return device

    def _runtime_context(self, session: ReproductionSession) -> VoiceRuntimeContext:
        raw=session.voice_runtime_context_json or {}
        return VoiceRuntimeContext(
            voice_vlan_id=str(raw.get('voice_vlan_id') or ''),voice_interface=str(raw.get('voice_interface') or ''),
            voice_device_ip=raw.get('voice_device_ip'),voice_gateway_ip=str(raw.get('voice_gateway_ip') or ''),
            interface_up=bool(raw.get('interface_up',False)),resolver_id=str(raw.get('resolver_id') or 'MOCK_VOICE_CONTEXT_V1'),
            resolver_version=str(raw.get('resolver_version') or '1.0.0'),
        )

    def _platform_event_source(self) -> str:
        return 'REAL_PLATFORM' if getattr(self.platform, 'supports_segmented_ring', False) else 'MOCK_PLATFORM'

    @staticmethod
    def _stage(profile: ReproductionProfileDefinition, stage: CaptureStage | str):
        stage=CaptureStage(stage)
        for item in profile.stages:
            if item.stage==stage: return item
        raise AppError('REPRODUCTION_PROFILE_CONTRACT_INVALID',details={'profile_id':profile.id,'missing_stage':stage.value})

    def start(self, db: Session, *, session: ReproductionSession, owner_worker: str='mock-reproduction-worker', actor: str|None=None) -> ReproductionSession:
        profile=self._profile(session)
        if ReproductionState(session.state) == ReproductionState.WAITING_EXTERNAL_ACTION:
            acquire_device_lock(db,session=session,owner_worker=owner_worker,lease_seconds=profile.timeouts.lease_seconds)
            transition_session(db,session,ReproductionEvent.RESOURCE_AVAILABLE,actor=actor,reason='external_action_completed')
        elif ReproductionState(session.state) == ReproductionState.WAITING_DEVICE_RESOURCE:
            try:
                acquire_device_lock(db,session=session,owner_worker=owner_worker,lease_seconds=profile.timeouts.lease_seconds)
            except AppError:
                return session
            transition_session(db,session,ReproductionEvent.RESOURCE_AVAILABLE,actor=actor,reason='device_resource_available')
        elif ReproductionState(session.state) == ReproductionState.CREATED:
            try:
                acquire_device_lock(db,session=session,owner_worker=owner_worker,lease_seconds=profile.timeouts.lease_seconds)
            except AppError as exc:
                if exc.code in {'DEVICE_DIAGNOSTIC_LOCKED','DEVICE_DIAGNOSTIC_QUARANTINED'}:
                    transition_session(
                        db,session,ReproductionEvent.DEVICE_RESOURCE_BUSY,actor=actor,
                        reason='device_quarantined' if exc.code=='DEVICE_DIAGNOSTIC_QUARANTINED' else 'device_busy',
                        payload={'error_code':exc.code,**exc.details},
                    )
                    return session
                raise
            transition_session(db,session,ReproductionEvent.START_ARMING,actor=actor,reason='autonomous_reproduction')
        elif ReproductionState(session.state) != ReproductionState.AUTO_ARMING:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'start'})
        return self._arm_current_stage(db,session=session,actor=actor)

    def _arm_current_stage(self, db: Session, *, session: ReproductionSession, actor: str|None=None) -> ReproductionSession:
        profile=self._profile(session); stage=self._stage(profile,session.capture_stage); device=self._device(db,session)
        session.cleanup_required=True
        session.cleanup_status=CleanupStatus.REQUIRED.value
        try:
            context=self.platform.resolve_voice_context(device)
            session.voice_runtime_context_json=context.as_dict()
            existing=db.scalar(select(VoiceRuntimeContextSnapshot).where(VoiceRuntimeContextSnapshot.session_id==session.id))
            if not existing:
                db.add(VoiceRuntimeContextSnapshot(
                    session_id=session.id,case_id=session.case_id,voice_vlan_id=context.voice_vlan_id,
                    voice_interface=context.voice_interface,voice_device_ip=context.voice_device_ip,
                    voice_gateway_ip=context.voice_gateway_ip,interface_up=context.interface_up,
                    resolver_id=context.resolver_id,resolver_version=context.resolver_version,snapshot_json=context.as_dict(),
                ))
            self.capture.state(db,session,pretrigger_ms=profile.ring.pretrigger_seconds*1000,segment_ms=profile.ring.segment_seconds*1000)
            observed=self.platform.arm(session_id=session.id,device=device,actions=stage.auto_arm_actions)
            decision=ArmReadinessBarrier.persist(db,session=session,profile=profile,observed=observed,required_channels=stage.required_channels)
            session.capture_completeness=(EvidenceCompleteness.COMPLETE.value if decision.status==ArmValidationStatus.PASSED
                                          else EvidenceCompleteness.PARTIAL.value if decision.status==ArmValidationStatus.PARTIAL
                                          else EvidenceCompleteness.UNAVAILABLE.value)
            if not decision.ready:
                transition_session(db,session,ReproductionEvent.ARM_FAILED,actor=actor,reason='arm_barrier_failed',
                                   payload={'failed_reasons':list(decision.failed_reasons)})
                session.terminal_reason='ARM_FAILED'
                return self.cleanup(db,session=session,actor=actor)
            if ReproductionState(session.state)==ReproductionState.ENHANCING:
                transition_session(db,session,ReproductionEvent.ENHANCEMENT_ARMED,actor=actor,reason='enhanced_arm_ready')
            else:
                transition_session(db,session,ReproductionEvent.ARM_READY,actor=actor,reason='arm_barrier_passed',
                                   payload={'validation_status':decision.status.value})
            transition_session(db,session,ReproductionEvent.WATCH_STARTED,actor=actor,reason='watching_started')
            db.flush(); return session
        except AppError as exc:
            if ReproductionState(session.state) in {ReproductionState.AUTO_ARMING,ReproductionState.ENHANCING}:
                transition_session(db,session,ReproductionEvent.ARM_FAILED,actor=actor,reason=exc.code,payload=exc.details)
            session.terminal_reason=exc.code
            session.terminal_detail_json=exc.details
            return self.cleanup(db,session=session,actor=actor)

    def heartbeat(self, db: Session, *, session: ReproductionSession) -> ReproductionSession:
        profile=self._profile(session)
        heartbeat_device_lock(db,session=session,lease_seconds=profile.timeouts.lease_seconds)
        return session

    def poll_capture_health(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        profile=self._profile(session)
        observed=self.platform.snapshot(session.id)
        decision=CaptureHealthMonitor.persist(db,session=session,profile=profile,observed=observed)
        if not decision.healthy:
            db.add(ReproductionEventRecord(
                session_id=session.id,case_id=session.case_id,event_type='CAPTURE_HEALTH_DEGRADED',source='CAPTURE_HEALTH_MONITOR',
                anchor_type=AnchorType.LIVE_ANOMALY.value,timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
                payload_json={'failed_required_channels':list(decision.failed_required_channels)},
            ))
        return decision

    def record_live_anomaly(self, db: Session, *, session: ReproductionSession, relative_ms: int, anomaly_type: str,
                            call_id: str|None=None, details: dict|None=None):
        row=ReproductionEventRecord(
            session_id=session.id,call_id=call_id,case_id=session.case_id,event_type=anomaly_type,source='LIVE_ANALYZER',
            anchor_type=AnchorType.LIVE_ANOMALY.value,session_relative_ms=int(relative_ms),
            timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,payload_json=details or {},
        )
        db.add(row); db.flush(); return row

    def record_activity(self, db: Session, *, session: ReproductionSession, relative_ms: int, source_event: str='FXS_OFFHOOK', actor: str|None=None) -> ReproductionAttempt:
        if ReproductionState(session.state)!=ReproductionState.WATCHING:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'record_activity'})
        attempt_no=(db.scalar(select(func.count(ReproductionAttempt.id)).where(ReproductionAttempt.session_id==session.id)) or 0)+1
        attempt=ReproductionAttempt(
            session_id=session.id,case_id=session.case_id,attempt_no=attempt_no,status=AttemptStatus.ACTIVE.value,
            start_anchor_type=source_event,start_anchor_ms=int(relative_ms),started_at=_utcnow(),
        )
        db.add(attempt); db.flush()
        db.add(ReproductionEventRecord(
            session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,event_type=source_event,source=self._platform_event_source(),
            anchor_type=AnchorType.PRIMARY_START.value,session_relative_ms=int(relative_ms),
            timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,payload_json={'runtime_anchor':True},
        ))
        if getattr(self.platform, 'supports_segmented_ring', False):
            # The real watcher has already been capturing bounded ring segments
            # before OFFHOOK. Freeze them immediately; never start a forward-looking
            # blocking "pretrigger" capture after the anchor.
            self.capture.freeze(db,session=session,anchor_ms=int(relative_ms),attempt_id=attempt.id)
        else:
            ctx=self._runtime_context(session); start=max(0,int(relative_ms)-self._profile(session).ring.pretrigger_seconds*1000)
            pre=self.platform.build_pretrigger_capture(context=ctx,start_ms=start,end_ms=int(relative_ms))
            self.capture.append_pcap(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.pcap,attempt_id=attempt.id)
            self.capture.append_log(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.debug_log,attempt_id=attempt.id)
            self.capture.freeze(db,session=session,anchor_ms=int(relative_ms),attempt_id=attempt.id)
        transition_session(db,session,ReproductionEvent.ACTIVITY,actor=actor,reason='earliest_low_level_anchor',
                           payload={'attempt_id':attempt.id,'anchor':source_event,'relative_ms':relative_ms})
        emit_event(db,event_type=EventType.REPRODUCTION_ATTEMPT_CHANGED,case_id=session.case_id,
                   entity_type='reproduction_attempt',entity_id=attempt.id,payload={'status':attempt.status,'attempt_no':attempt.attempt_no})
        log.info('[repro %s] OFFHOOK -> attempt no=%s state=%s', session.id[:8], attempt.attempt_no, ReproductionState(session.state).value)
        return attempt

    def record_fxs_event(self, db: Session, *, session: ReproductionSession, event, actor: str|None=None) -> ReproductionAttempt | None:
        """Bridge a real DUT FXS event from the monitor into the reproduction state machine.

        OFFHOOK starts a new attempt (primary start anchor) if the session is watching;
        ONHOOK ends the active attempt without a call; DTMF is recorded as a dial signal.
        Returns the attempt when one was created or ended, else None.
        """
        relative_ms=int((event.timestamp and 0) or 0)  # replaced below by clock when available
        if self.fxs_event_monitor is not None and self.fxs_event_monitor.relative_ms is not None:
            relative_ms=self.fxs_event_monitor.relative_ms()
        state=ReproductionState(session.state)
        if event.event=='OFFHOOK':
            if state!=ReproductionState.WATCHING:
                return None
            return self.record_activity(db,session=session,relative_ms=relative_ms,source_event='FXS_OFFHOOK',actor=actor)
        if event.event=='ONHOOK':
            attempt=db.scalar(select(ReproductionAttempt).where(
                ReproductionAttempt.session_id==session.id,
                ReproductionAttempt.status==AttemptStatus.ACTIVE.value).order_by(ReproductionAttempt.attempt_no.desc()))
            if not attempt:
                return None
            if ReproductionState(session.state)==ReproductionState.ACTIVITY_DETECTED:
                return self.end_activity_without_call(db,session=session,attempt_id=attempt.id,
                                                      relative_ms=relative_ms,end_anchor='FXS_ONHOOK',actor=actor)
            return None
        if event.event=='DTMF':
            # A digit is meaningful both while dialing (ACTIVITY_DETECTED) and once the
            # call is up (CALL_DETECTED/CAPTURING, e.g. IVR input). Dropping the latter
            # made in-call DTMF unobservable, so record across the whole off-hook span.
            if state not in {ReproductionState.ACTIVITY_DETECTED,ReproductionState.CALL_DETECTED,
                             ReproductionState.CAPTURING}:
                return None
            attempt=db.scalar(select(ReproductionAttempt).where(
                ReproductionAttempt.session_id==session.id,
                ReproductionAttempt.status==AttemptStatus.ACTIVE.value).order_by(ReproductionAttempt.attempt_no.desc()))
            # Attribute to the Call when it already exists at write time. Binding
            # trails physical answer by ~8s (segmented capture), so digits pressed
            # during a call usually arrive before the Call row; correct in-call
            # attribution for those needs post-hoc PCAP reconciliation (pending).
            call=None
            if attempt is not None:
                call=db.scalar(select(ReproductionCall).where(
                    ReproductionCall.session_id==session.id,
                    ReproductionCall.attempt_id==attempt.id,
                    ReproductionCall.status==ReproductionCallStatus.ACTIVE.value).order_by(ReproductionCall.call_no.desc()))
            in_call=call is not None
            db.add(ReproductionEventRecord(
                session_id=session.id,attempt_id=attempt.id if attempt else None,
                call_id=call.id if call else None,
                case_id=session.case_id,event_type='FXS_DTMF',source='REAL_PLATFORM',
                anchor_type=AnchorType.PRIMARY_START.value,session_relative_ms=int(relative_ms),
                timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,
                payload_json={'digit':event.digit,'in_call':in_call}))
            db.flush()
            return None
        return None

    def end_activity_without_call(self, db: Session, *, session: ReproductionSession, attempt_id: str, relative_ms: int, end_anchor: str='FXS_ONHOOK', actor: str|None=None) -> ReproductionAttempt:
        attempt=db.get(ReproductionAttempt,attempt_id)
        if not attempt or attempt.session_id!=session.id: raise AppError('REPRODUCTION_ATTEMPT_NOT_FOUND')
        if attempt.status!=AttemptStatus.ACTIVE.value: return attempt
        attempt.status=AttemptStatus.INVALID.value; attempt.valid=False; attempt.end_anchor_type=end_anchor; attempt.end_anchor_ms=int(relative_ms); attempt.ended_at=_utcnow()
        db.add(ReproductionEventRecord(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,event_type=end_anchor,
                                       source=self._platform_event_source(),anchor_type=AnchorType.PRIMARY_END.value,session_relative_ms=int(relative_ms),
                                       timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,payload_json={'valid_call':False}))
        self.capture.reset_after_attempt(db,session=session,invalid=True)
        transition_session(db,session,ReproductionEvent.WATCH_STARTED,actor=actor,reason='invalid_attempt_continue_watching',payload={'attempt_id':attempt.id})
        log.info('[repro %s] ONHOOK no-call -> attempt no=%s INVALID -> WATCHING', session.id[:8], attempt.attempt_no)
        return attempt

    def bind_call(self, db: Session, *, session: ReproductionSession, relative_ms: int, external_call_ref: str|None=None, actor: str|None=None,
                  binding_event: str|None=None) -> ReproductionCall:
        state=ReproductionState(session.state)
        attempt=db.scalar(select(ReproductionAttempt).where(
            ReproductionAttempt.session_id==session.id,ReproductionAttempt.status==AttemptStatus.ACTIVE.value).order_by(ReproductionAttempt.attempt_no.desc()))
        pre=None
        if state==ReproductionState.WATCHING and not attempt:
            # Low-level anchor was missed; freeze/bind on INVITE and mark it reconstructable offline.
            attempt_no=(db.scalar(select(func.count(ReproductionAttempt.id)).where(ReproductionAttempt.session_id==session.id)) or 0)+1
            attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=attempt_no,status=AttemptStatus.ACTIVE.value,
                                        start_anchor_type='SIP_INVITE_FALLBACK',start_anchor_ms=int(relative_ms),details_json={'low_level_anchor_missed':True})
            db.add(attempt); db.flush()
            if getattr(self.platform, 'supports_segmented_ring', False):
                self.capture.freeze(db,session=session,anchor_ms=int(relative_ms),attempt_id=attempt.id)
            else:
                ctx=self._runtime_context(session); start=max(0,int(relative_ms)-self._profile(session).ring.pretrigger_seconds*1000)
                pre=self.platform.build_pretrigger_capture(context=ctx,start_ms=start,end_ms=int(relative_ms))
                self.capture.append_pcap(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.pcap,attempt_id=attempt.id)
                self.capture.append_log(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.debug_log,attempt_id=attempt.id)
                self.capture.freeze(db,session=session,anchor_ms=int(relative_ms),attempt_id=attempt.id)
        if state not in {ReproductionState.WATCHING,ReproductionState.ACTIVITY_DETECTED}:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'bind_call'})
        call_no=(db.scalar(select(func.count(ReproductionCall.id)).where(ReproductionCall.session_id==session.id)) or 0)+1
        call=ReproductionCall(session_id=session.id,attempt_id=attempt.id if attempt else None,case_id=session.case_id,
                              call_no=call_no,external_call_ref=external_call_ref,status=ReproductionCallStatus.ACTIVE.value)
        db.add(call); db.flush()
        if getattr(self.platform, 'supports_segmented_ring', False) and attempt is not None:
            # Associate every frozen segment from the active attempt with the
            # deterministic Call. This retains the pre-OFFHOOK/dialing context and
            # prevents the call merger from selecting only the final RTP segment.
            for segment in db.scalars(select(ReproductionCaptureSegment).where(
                ReproductionCaptureSegment.session_id==session.id,
                ReproductionCaptureSegment.attempt_id==attempt.id,
                ReproductionCaptureSegment.call_id.is_(None),
            )):
                segment.call_id=call.id
        # Backfill FXS DTMF events that were recorded BEFORE this Call row existed.
        # Call binding trails the physical answer by ~1 segment (~8s) because it
        # waits on downloaded PCAP, so dialing and early in-call digits (IVR input,
        # in-call keys) are typically recorded with call_id=NULL. Once the Call is
        # bound, attribute every FXS_DTMF of this attempt to the Call. Real session
        # d60b2f5b (RP-D08) proved the digits are fully captured in PCM; only the
        # call_id association was missing, making in-call DTMF unobservable at the
        # event layer (ledger KNOWN-DEFECT #2).
        if attempt is not None:
            for ev in db.scalars(select(ReproductionEventRecord).where(
                ReproductionEventRecord.session_id==session.id,
                ReproductionEventRecord.attempt_id==attempt.id,
                ReproductionEventRecord.event_type=='FXS_DTMF',
                ReproductionEventRecord.call_id.is_(None),
            )):
                ev.call_id=call.id
                ev.payload_json={**(ev.payload_json or {}), 'in_call': True}
        # Cache the dialing-window pretrigger under the new call_id so the real
        # platform's final merged call.pcap includes the dialing DTMF/silence (the
        # pretrigger is captured above, before the call row existed).
        if pre is not None:
            self.platform.cache_pretrigger(call_id=call.id, pcap=pre.pcap)
        if attempt: attempt.valid=True
        binding = binding_event or self._profile(session).call_binding_event or 'SIP_INVITE'
        db.add(ReproductionEventRecord(session_id=session.id,attempt_id=attempt.id if attempt else None,call_id=call.id,case_id=session.case_id,
                                       event_type=binding,
                                       source='PCAP_SIGNAL_OBSERVER' if getattr(self.platform, 'supports_segmented_ring', False) else 'MOCK_PCAP',
                                       anchor_type=AnchorType.CALL_BINDING.value,
                                       session_relative_ms=int(relative_ms),timestamp_source=TimestampSource.PCAP.value,uncertainty_ms=1,
                                       payload_json={'external_call_ref':external_call_ref,'binding_event':binding}))
        transition_session(db,session,ReproductionEvent.CALL_BOUND,actor=actor,reason='call_binding_event',payload={'call_id':call.id,'call_no':call.call_no})
        transition_session(db,session,ReproductionEvent.CAPTURE_STARTED,actor=actor,reason='call_scope_capture')
        if not getattr(self.platform, 'supports_segmented_ring', False):
            ctx=self._runtime_context(session); probe=self.platform.build_live_probe(context=ctx,start_ms=int(relative_ms),call_id=call.id)
            pseg=self.capture.append_pcap(db,session=session,start_ms=int(relative_ms),end_ms=int(relative_ms)+500,data=probe.pcap,attempt_id=attempt.id if attempt else None,call_id=call.id,metadata={'mock_probe_only':True,'phase':'LIVE_PROBE'})
            self.capture.preserve_new_segment(db,session=session,row=pseg)
            lseg=self.capture.append_log(db,session=session,start_ms=int(relative_ms),end_ms=int(relative_ms)+500,data=probe.debug_log,attempt_id=attempt.id if attempt else None,call_id=call.id,metadata={'phase':'LIVE_PROBE'})
            self.capture.preserve_new_segment(db,session=session,row=lseg)
            if pseg.evidence_id:
                live_ev=db.get(Evidence,pseg.evidence_id)
                if live_ev:
                    call.live_summary_json=self.live_analyzer.run(db,session=session,call=call,pcap_path=__import__('pathlib').Path(pseg.local_path),input_evidence=live_ev)
        emit_event(db,event_type=EventType.REPRODUCTION_CALL_CHANGED,case_id=session.case_id,entity_type='reproduction_call',entity_id=call.id,
                   payload={'status':call.status,'call_no':call.call_no,'live_summary':call.live_summary_json})
        log.info('[repro %s] CALL_BOUND call=%s attempt=%s binding=%s', session.id[:8], call.id[:8], (attempt.id[:8] if attempt else None), binding)
        return call

    def _compensate_call_capture(self, db: Session, *, session: ReproductionSession, call: ReproductionCall,
                                 start_ms: int, end_ms: int, signal) -> object | None:
        """Capture-completeness guard for the final call capture.

        When the platform's build_call_capture returns an empty pcap (mirror stream
        already stopped at hangup / window landed in a silent gap), retry a wider
        post-call window and, as a last resort, reconstruct media evidence from any
        retained PCAP segments so CALL_QUICK never analyzes an empty capture.
        Returns a capture-like object (with ``pcap``/``debug_log``) or None.
        """
        ctx=self._runtime_context(session)
        for _attempt in range(2):
            try:
                cap=self.platform.build_call_capture(context=ctx,start_ms=start_ms,end_ms=end_ms,
                    call_id=call.id,profile_id=session.profile_key,signal=signal)
            except Exception:
                cap=None
            if cap is not None and getattr(cap,'pcap',None) and len(cap.pcap)>24:
                return cap
        try:
            from app.db.models import ReproductionCaptureSegment
            from app.reproduction.pcap_codec import merge_classic_pcaps
            from app.reproduction.real_platform import RealCapture
            rows=list(db.scalars(select(ReproductionCaptureSegment).where(
                ReproductionCaptureSegment.session_id==session.id,
                ReproductionCaptureSegment.channel==CaptureChannel.PCAP.value,
                ReproductionCaptureSegment.retained.is_(True),
            ).order_by(ReproductionCaptureSegment.segment_no)))
            paths=[r.local_path for r in rows if r.local_path]
            if paths:
                merged_path=self.capture._session_dir(session.id)/'calls'/call.id/'compensated.pcap'
                merged_path.parent.mkdir(parents=True,exist_ok=True)
                merge_classic_pcaps(paths,merged_path)
                merged=merged_path.read_bytes()
            else:
                merged=b''
            if len(merged)>24:
                return RealCapture(pcap=merged, debug_log=b'')
        except Exception:
            pass
        return None

    def end_call(self, db: Session, *, session: ReproductionSession, call_id: str, relative_ms: int,
                 signal: QuickAnalysisInput, end_anchor: str='FXS_ONHOOK', actor: str|None=None) -> tuple[ReproductionCall,SufficiencyDecision]:
        call=db.get(ReproductionCall,call_id)
        if not call or call.session_id!=session.id: raise AppError('REPRODUCTION_CALL_NOT_FOUND')
        if ReproductionState(session.state) not in {ReproductionState.CAPTURING,ReproductionState.CALL_DETECTED}:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'end_call'})
        call.status=ReproductionCallStatus.ENDED.value; call.ended_at=_utcnow()
        if call.attempt_id:
            attempt=db.get(ReproductionAttempt,call.attempt_id)
            if attempt:
                attempt.valid=True; attempt.status=AttemptStatus.COMPLETED.value; attempt.end_anchor_type=end_anchor; attempt.end_anchor_ms=int(relative_ms); attempt.ended_at=_utcnow()
        db.add(ReproductionEventRecord(session_id=session.id,attempt_id=call.attempt_id,call_id=call.id,case_id=session.case_id,
                                       event_type=end_anchor,source=self._platform_event_source(),anchor_type=AnchorType.PRIMARY_END.value,
                                       session_relative_ms=int(relative_ms),timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
                                       uncertainty_ms=1,payload_json={}))
        transition_session(db,session,ReproductionEvent.CALL_ENDED,actor=actor,reason='call_end_anchor',payload={'call_id':call.id})
        transition_session(db,session,ReproductionEvent.POST_CAPTURE_STARTED,actor=actor,reason='post_capture_window')
        call.status=ReproductionCallStatus.ANALYZING.value
        bind_event=db.scalar(select(ReproductionEventRecord).where(
            ReproductionEventRecord.call_id==call.id,
            ReproductionEventRecord.anchor_type==AnchorType.CALL_BINDING.value,
        ).order_by(ReproductionEventRecord.session_relative_ms))
        call_start_ms=int(bind_event.session_relative_ms if bind_event and bind_event.session_relative_ms is not None else max(0,int(relative_ms)-1200))
        scenario=None
        if not getattr(self.platform, 'supports_segmented_ring', False):
            ctx=self._runtime_context(session)
            scenario=self.platform.build_call_capture(
                context=ctx,start_ms=call_start_ms,end_ms=int(relative_ms),
                call_id=call.id,profile_id=session.profile_key,signal=signal)
        # Capture-completeness guard: an empty final pcap (mirror stream already
        # stopped at hangup / window landed in a silent gap) must never become the
        # sole CALL_FINAL evidence. Compensate before appending so CALL_QUICK never
        # analyzes an empty capture.
        if (not getattr(self.platform, 'supports_segmented_ring', False)
                and (not getattr(scenario,'pcap',None) or len(scenario.pcap)<=24)):
            scenario=self._compensate_call_capture(db,session=session,call=call,start_ms=call_start_ms,end_ms=int(relative_ms),signal=signal)
        if scenario is not None:
            pseg=self.capture.append_pcap(db,session=session,start_ms=call_start_ms,end_ms=int(relative_ms)+self._profile(session).timeouts.post_capture_seconds*1000,data=scenario.pcap,attempt_id=call.attempt_id,call_id=call.id,metadata={'mock_final_call':True,'phase':'CALL_FINAL'})
            self.capture.preserve_new_segment(db,session=session,row=pseg)
            lseg=self.capture.append_log(db,session=session,start_ms=call_start_ms,end_ms=int(relative_ms)+self._profile(session).timeouts.post_capture_seconds*1000,data=scenario.debug_log,attempt_id=call.attempt_id,call_id=call.id,metadata={'phase':'CALL_FINAL'})
            self.capture.preserve_new_segment(db,session=session,row=lseg)
        # When compensation also failed, no empty final segment is appended: the
        # capture pipeline falls back to the retained in-call LIVE_PROBE segments
        # (or raises CALL_CAPTURE_SEGMENTS_MISSING -> task autoretry).
        call_pcap,call_evidence=self.capture.build_call_capture(db,session=session,call=call)
        result=self.quick_analyzer.run(db,session=session,call=call,signal=signal,pcap_path=call_pcap,pcap_evidence=call_evidence)
        call.status=ReproductionCallStatus.ANALYZED.value
        call.verdict=result.verdict.value; call.role=result.role.value
        call.quick_analysis_json={
            'analyzer_run_id':result.analyzer_run_id,'findings':list(result.findings),'metrics':result.metrics,
            'hard_contradiction':result.hard_contradiction,'capture_recovery_required':result.capture_recovery_required,
            'external_action_required':result.external_action_required,'input_evidence_ids':list(result.input_evidence_ids),
            'output_evidence_ids':list(result.output_evidence_ids),'analysis_summary':result.analysis_summary,
        }
        # Reconcile the authoritative PCM-media DTMF sequences into the event record
        # so fast key presses that the device FXS event report drops (DSP tone
        # duration/inter-digit thresholds) are still reflected in the DTMF record.
        self._reconcile_pcm_dtmf_events(db,session=session,call=call,result=result)
        if result.role==CallRole.TARGET and session.primary_target_call_id is None:
            session.primary_target_call_id=call.id
            emit_event(db,event_type=EventType.TARGET_CONFIRMED,case_id=session.case_id,entity_type='reproduction_call',entity_id=call.id,
                       payload={'session_id':session.id,'call_no':call.call_no})
        decision=self._evaluate_session_sufficiency(db,session=session,current=result)
        session.evidence_sufficiency=decision.status.value
        q=db.scalar(select(DiagnosticQuestion).where(DiagnosticQuestion.session_id==session.id).order_by(DiagnosticQuestion.created_at.desc()))
        if q:
            answer={'status':decision.status.value,'next_action':decision.next_action.value,'reasons':list(decision.reasons),
                    'missing_channels':list(decision.missing_channels),'missing_findings':list(decision.missing_findings),
                    'findings':list(result.findings)}
            if decision.sufficient:
                self.questions.answer(
                    db,question=q,answer=answer,
                    evidence_refs=[{'evidence_id':x,'level':'L1'} for x in dict.fromkeys([*result.input_evidence_ids,*result.output_evidence_ids])],
                    actor=actor,
                )
            else:
                q.answer_json=answer
        self.capture.reset_after_attempt(db,session=session,invalid=False)
        self._after_call(db,session=session,call=call,decision=decision,actor=actor)
        log.info('[repro %s] CALL_ENDED call=%s verdict=%s findings=%s sufficiency=%s', session.id[:8], call.id[:8],
                 result.verdict.value, sorted(result.findings), decision.status.value)
        emit_event(db,event_type=EventType.REPRODUCTION_CALL_CHANGED,case_id=session.case_id,entity_type='reproduction_call',entity_id=call.id,
                   payload={'status':call.status,'verdict':call.verdict,'role':call.role,'sufficiency':decision.status.value})
        return call,decision

    def _reconcile_pcm_dtmf_events(self, db: Session, *, session: ReproductionSession, call: ReproductionCall, result) -> None:
        """Record PCM-media DTMF sequences as supplementary reproduction events.

        The device FXS event report (``FXS_DTMF``) drops digits under fast key
        presses (the DSP applies tone-duration / inter-digit thresholds); the PCM
        media still carries those tones. Conversely the PCM media stream can end
        before the physical hang-up, so FXS can hold digits the media lacks. No
        single source is complete, so we record the media-truth sequences as
        supplementary ``PCM_DTMF_SEQUENCE`` (source ``PCM_MEDIA_ANALYSIS``) events;
        the complete DTMF record is the union of ``FXS_DTMF`` and
        ``PCM_DTMF_SEQUENCE``. P2-2 / RP-D06 fast-dial verification relies on this.
        """
        seqs=getattr(result,'pcm_dtmf_sequences',()) or ()
        if not seqs:
            return
        existing=set()
        for row in db.scalars(select(ReproductionEventRecord).where(
                ReproductionEventRecord.session_id==session.id,
                ReproductionEventRecord.event_type=='PCM_DTMF_SEQUENCE',
                ReproductionEventRecord.call_id==call.id)):
            existing.add((row.event_type,(row.payload_json or {}).get('digits')))
        for seq in seqs:
            digits=seq.get('digits')
            if not digits:
                continue
            if ('PCM_DTMF_SEQUENCE',digits) in existing:
                continue
            db.add(ReproductionEventRecord(
                session_id=session.id,attempt_id=call.attempt_id,call_id=call.id,
                case_id=session.case_id,event_type='PCM_DTMF_SEQUENCE',
                source='PCM_MEDIA_ANALYSIS',anchor_type=AnchorType.PRIMARY_START.value,
                session_relative_ms=int((seq.get('start_seconds') or 0)*1000),
                timestamp_source=TimestampSource.PCAP.value,uncertainty_ms=10,
                payload_json={'digits':digits,'event_count':seq.get('event_count'),
                              'min_confidence':seq.get('min_confidence'),'tap':seq.get('tap'),
                              'session_index':seq.get('session_index'),'supplementary':True}))
        db.flush()

    def _evaluate_session_sufficiency(self, db: Session, *, session: ReproductionSession, current: QuickAnalysisResult) -> SufficiencyDecision:
        profile=self._profile(session)
        health=list(db.scalars(select(CaptureChannelHealth).where(CaptureChannelHealth.session_id==session.id)))
        channel_complete={x.channel:(x.status==ChannelHealth.HEALTHY.value) for x in health}
        calls=list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id==session.id,ReproductionCall.status==ReproductionCallStatus.ANALYZED.value)))
        findings=set()
        hard=False; recovery=False; external=False
        for c in calls:
            qa=c.quick_analysis_json or {}
            findings.update(qa.get('findings') or [])
            hard=hard or bool(qa.get('hard_contradiction'))
            recovery=recovery or bool(qa.get('capture_recovery_required'))
            external=external or bool(qa.get('external_action_required'))
        target=any(c.verdict==CallVerdict.MATCH.value for c in calls)
        control=any(c.verdict==CallVerdict.NO_MATCH.value for c in calls)
        stages=[x.stage for x in profile.stages]
        current_stage=CaptureStage(session.capture_stage)
        enhancement_available=any(stages.index(x)>stages.index(current_stage) for x in stages if current_stage in stages)
        return self.sufficiency.evaluate(profile,channel_complete=channel_complete,findings=findings,target_match=target,
                                         control_present=control,hard_contradiction=hard,capture_recovery_required=recovery,
                                         external_action_required=external,enhancement_available=enhancement_available)

    def _after_call(self, db: Session, *, session: ReproductionSession, call: ReproductionCall, decision: SufficiencyDecision, actor: str|None=None):
        profile=self._profile(session)
        calls=list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id==session.id,ReproductionCall.status==ReproductionCallStatus.ANALYZED.value)))
        target_count=sum(1 for c in calls if c.verdict==CallVerdict.MATCH.value)
        has_control=any(c.verdict==CallVerdict.NO_MATCH.value for c in calls)
        should_finish=(decision.sufficient or
                       (profile.end_policy==EndPolicy.FIRST_TARGET and call.verdict==CallVerdict.MATCH.value) or
                       (profile.end_policy==EndPolicy.TARGET_COUNT and target_count>=profile.target_count) or
                       (profile.end_policy==EndPolicy.CONTROL_TARGET_PAIR and target_count>0 and has_control) or
                       len(calls)>=profile.max_calls)
        if should_finish:
            session.terminal_reason='EVIDENCE_SUFFICIENT' if decision.sufficient else 'END_POLICY_REACHED'
            self.cleanup(db,session=session,actor=actor)
            return
        if decision.next_action==EvidenceGapAction.EXTERNAL_ACTION_REQUIRED:
            session.terminal_reason='EXTERNAL_ACTION_REQUIRED'
            self.cleanup(db,session=session,actor=actor,external_wait=True)
            return
        if decision.next_action==EvidenceGapAction.ENHANCE_CAPTURE:
            self._enhance(db,session=session,actor=actor)
            return
        if decision.next_action==EvidenceGapAction.CAPTURE_RECOVERY:
            self._recover_capture(db,session=session,actor=actor)
            return
        transition_session(db,session,ReproductionEvent.WATCH_STARTED,actor=actor,reason='insufficient_retry_same_capture')

    def _next_stage(self, profile: ReproductionProfileDefinition, current: CaptureStage) -> CaptureStage | None:
        stages=[x.stage for x in profile.stages]
        try: idx=stages.index(current)
        except ValueError: return None
        return stages[idx+1] if idx+1<len(stages) else None

    def _enhance(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        profile=self._profile(session); next_stage=self._next_stage(profile,CaptureStage(session.capture_stage))
        if not next_stage:
            transition_session(db,session,ReproductionEvent.WATCH_STARTED,actor=actor,reason='no_enhancement_stage_available')
            return
        transition_session(db,session,ReproductionEvent.ENHANCEMENT_STARTED,actor=actor,reason='evidence_gap_enhance',payload={'next_stage':next_stage.value})
        session.capture_stage=next_stage.value
        self._arm_current_stage(db,session=session,actor=actor)

    def _recover_capture(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        transition_session(db,session,ReproductionEvent.CAPTURE_RECOVERY_STARTED,actor=actor,reason='capture_recovery')
        self._arm_current_stage(db,session=session,actor=actor)

    def watch_timeout(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        if ReproductionState(session.state) not in {ReproductionState.WATCHING,ReproductionState.ACTIVITY_DETECTED}:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'watch_timeout'})
        transition_session(db,session,ReproductionEvent.WATCH_TIMEOUT,actor=actor,reason='watch_timeout')
        session.terminal_reason='WATCH_TIMEOUT'
        return self.cleanup(db,session=session,actor=actor)

    def capture_timeout(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        if ReproductionState(session.state) not in {ReproductionState.CALL_DETECTED,ReproductionState.CAPTURING}:
            raise AppError('REPRODUCTION_TRANSITION_NOT_ALLOWED',details={'state':session.state,'operation':'capture_timeout'})
        transition_session(db,session,ReproductionEvent.CAPTURE_TIMEOUT,actor=actor,reason='capture_timeout')
        session.terminal_reason='CAPTURE_TIMEOUT'
        return self.cleanup(db,session=session,actor=actor)

    def cancel(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        state=ReproductionState(session.state)
        if state in TERMINAL_STATES: return session
        session.terminal_reason='CANCEL_REQUESTED'
        transition_session(db,session,ReproductionEvent.CANCEL_REQUESTED,actor=actor,reason='user_stop_requested')
        return self.cleanup(db,session=session,actor=actor,already_cleanup_state=True)

    def cleanup(self, db: Session, *, session: ReproductionSession, actor: str|None=None,
                external_wait: bool=False, already_cleanup_state: bool=False) -> ReproductionSession:
        profile=self._profile(session); device=self._device(db,session)
        if not already_cleanup_state and ReproductionState(session.state)!=ReproductionState.CLEANUP:
            transition_session(db,session,ReproductionEvent.CLEANUP_STARTED,actor=actor,reason=session.terminal_reason or 'cleanup_required')
        session.cleanup_status=CleanupStatus.RUNNING.value
        run_no=(db.scalar(select(func.count(CleanupRun.id)).where(CleanupRun.session_id==session.id)) or 0)+1
        run=CleanupRun(session_id=session.id,run_no=run_no,status=CleanupRunStatus.RUNNING.value,started_at=_utcnow())
        db.add(run); db.flush()
        actions=list(profile.cleanup_actions)
        guard_snapshot={}; guard_meta={}
        if self.pcm_cleanup_guard is not None:
            actions, guard_snapshot, guard_meta=self._run_pcm_guard(db,session=session,actions=actions)
        snapshot=self.platform.cleanup(session_id=session.id,device=device,actions=actions)
        if guard_snapshot:
            reverse=dict(snapshot.get('reverse_validation') or {})
            reverse.update(guard_snapshot)
            snapshot['reverse_validation']=reverse
            final=dict(snapshot.get('final') or {})
            final.update(guard_snapshot)
            snapshot['final']=final
        run.action_results_json={
            'actions': actions,
            'platform_id': getattr(self.platform, 'platform_id', type(self.platform).__name__),
            'mock_platform': not getattr(self.platform, 'supports_segmented_ring', False),
            'pcm_guard': guard_meta or None,
        }
        decision=CleanupReadinessBarrier.persist(db,session=session,run=run,snapshot=snapshot)
        if decision.verified:
            release_device_lock(db,session=session,cleanup_verified=True)
            session.cleanup_required=False
            if external_wait or session.terminal_reason=='EXTERNAL_ACTION_REQUIRED':
                transition_session(db,session,ReproductionEvent.CLEANUP_VERIFIED_EXTERNAL_WAIT,actor=actor,reason='safe_external_wait')
                return session
            transition_session(db,session,ReproductionEvent.CLEANUP_VERIFIED,actor=actor,reason='cleanup_reverse_validation_passed')
            return self._finalize(db,session=session,actor=actor)
        transition_session(db,session,ReproductionEvent.CLEANUP_FAILED,actor=actor,reason='cleanup_reverse_validation_failed',
                           payload={'failures':list(decision.failures)})
        quarantine_device_lock(db,session=session)
        return session

    def _prior_pcm_off_state(self, db: Session, *, session: ReproductionSession) -> dict[str, bool]:
        """Restore per-channel OFF history from the most recent completed CleanupRun.

        A watchdog retry after a worker restart must never issue a second PCM OFF for a
        channel that already executed its only permitted OFF. The current RUNNING run is
        excluded because its action_results_json is not populated yet.
        """
        prior=db.scalar(
            select(CleanupRun)
            .where(CleanupRun.session_id==session.id, CleanupRun.status != CleanupRunStatus.RUNNING.value)
            .order_by(CleanupRun.run_no.desc())
        )
        if not prior or not prior.action_results_json:
            return {}
        guard_meta=prior.action_results_json.get('pcm_guard') or {}
        return {channel: bool(v.get('off_already_executed', False)) for channel, v in guard_meta.items()}

    def _run_pcm_guard(self, db: Session, *, session: ReproductionSession, actions: list[str]):
        """Clean PCM channels through the injected guard instead of a blind OFF.

        The guard probes each PCM UDP stream; a quiet stream skips the non-idempotent OFF,
        and a stream that already executed its single OFF is never sent a second one. The
        remaining actions are delegated to the platform adapter.
        """
        ctx=self._runtime_context(session)
        if not ctx.voice_interface or not ctx.voice_gateway_ip:
            raise AppError('PCM_GUARD_RUNTIME_CONTEXT_MISSING', details={'session_id':session.id})
        prior_off=self._prior_pcm_off_state(db, session=session)
        remaining: list[str] = []
        guard_snapshot: dict[str, dict] = {}
        guard_meta: dict[str, dict] = {}
        for action in actions:
            channel=next((ch for ch, a in _PCM_CHANNEL_TO_STOP_ACTION.items() if a == action), None)
            if channel is None:
                remaining.append(action)
                continue
            off_already=bool(prior_off.get(channel, False))
            result=self.pcm_cleanup_guard.cleanup_channel(
                channel=channel,
                voice_interface=ctx.voice_interface,
                voice_gateway_ip=ctx.voice_gateway_ip,
                off_already_executed=off_already,
            )
            guard_snapshot[channel]=_pcm_guard_channel_snapshot(result)
            guard_meta[channel]=result.as_dict()
            guard_meta[channel]['off_already_executed']=off_already or result.off_executed or result.retry_blocked
        return remaining, guard_snapshot, guard_meta

    def retry_cleanup(self, db: Session, *, session: ReproductionSession, actor: str|None=None):
        if ReproductionState(session.state) not in {ReproductionState.CLEANUP_FAILED,ReproductionState.CLEANUP_DEGRADED,ReproductionState.ORPHANED}:
            return session
        transition_session(db,session,ReproductionEvent.CLEANUP_STARTED,actor=actor,reason='cleanup_watchdog_retry')
        return self.cleanup(db,session=session,actor=actor,already_cleanup_state=True)

    def _finalize(self, db: Session, *, session: ReproductionSession, actor: str|None=None) -> ReproductionSession:
        finalization=self.capture.finalize_session(db,session=session)
        session.terminal_detail_json={**(session.terminal_detail_json or {}),'evidence_finalization':finalization}
        if session.terminal_reason=='CANCEL_REQUESTED':
            transition_session(db,session,ReproductionEvent.CANCELLED,actor=actor,reason='cancelled_after_cleanup')
            return session
        transition_session(db,session,ReproductionEvent.ANALYSIS_STARTED,actor=actor,reason='finalize_evidence_bundle')
        if session.evidence_sufficiency==EvidenceSufficiency.SUFFICIENT.value:
            transition_session(db,session,ReproductionEvent.SESSION_COMPLETED,actor=actor,reason='evidence_sufficient')
        else:
            transition_session(db,session,ReproductionEvent.SESSION_PARTIAL,actor=actor,reason=session.terminal_reason or 'evidence_insufficient')
        return session

    def create_case_retry(self, db: Session, *, session: ReproductionSession, actor: str|None=None) -> ReproductionSession | None:
        profile=self._profile(session)
        if session.terminal_reason not in set(profile.retry.retryable_reasons):
            return None
        count=db.scalar(select(func.count(ReproductionSession.id)).where(
            ReproductionSession.case_id==session.case_id,ReproductionSession.device_id==session.device_id,
            ReproductionSession.profile_key==session.profile_key)) or 0
        if count>=profile.retry.max_sessions:
            return None
        return self.create_session(db,case_id=session.case_id,profile_id=session.profile_key,device_id=session.device_id,
                                   actor=actor,retry_parent_session_id=session.id)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    AnchorType, ArmValidationStatus, AttemptStatus, CallRole, CallVerdict, CaptureChannel, CaptureStage,
    ChannelHealth, CleanupRunStatus, CleanupStatus, DiagnosticQuestionState, EndPolicy,
    EventType, EvidenceCompleteness, EvidenceGapAction, EvidenceSufficiency, ReproductionCallStatus,
    ReproductionEvent, ReproductionProfileStatus, ReproductionState, TimestampSource,
)
from app.core.errors import AppError
from app.db.models import (
    Case, CaseDevice, CaptureChannelHealth, CleanupRun, DiagnosticQuestion, ReproductionAttempt,
    ReproductionCall, ReproductionEventRecord, ReproductionProfile, ReproductionProfileVersion,
    ReproductionSession, VoiceRuntimeContextSnapshot, Evidence,
)
from app.reproduction.barriers import ArmReadinessBarrier, CleanupReadinessBarrier
from app.reproduction.locks import acquire_device_lock, heartbeat_device_lock, release_device_lock
from app.reproduction.health import CaptureHealthMonitor
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.live import LiveReproductionAnalyzer
from app.reproduction.mock_platform import MockReproductionPlatform, VoiceRuntimeContext
from app.reproduction.profile import LoadedReproductionProfile, ReproductionProfileDefinition, ReproductionProfileRegistry
from app.reproduction.quick import MockCallQuickAnalyzer, QuickAnalysisInput, QuickAnalysisResult
from app.reproduction.state_machine import TERMINAL_STATES, transition_session
from app.reproduction.sufficiency import EvidenceSufficiencyEvaluator, SufficiencyDecision
from app.reproduction.question_graph import DiagnosticQuestionGraph
from app.services.audit import audit
from app.services.events import emit_event


def _utcnow():
    return datetime.now(timezone.utc)


class ReproductionOrchestrator:
    """Persistent M6.2 orchestration core using a deterministic Mock Platform.

    No real device command exists in this class. Production platform integration is blocked on EC-02.
    """

    def __init__(self, *, registry: ReproductionProfileRegistry | None = None, platform: MockReproductionPlatform | None = None, capture_pipeline: ReproductionCapturePipeline | None = None):
        self.registry=registry or ReproductionProfileRegistry()
        self.platform=platform or MockReproductionPlatform()
        self.capture= capture_pipeline or ReproductionCapturePipeline()
        self.quick_analyzer=MockCallQuickAnalyzer(storage=self.capture.storage)
        self.live_analyzer=LiveReproductionAnalyzer(storage=self.capture.storage)
        self.sufficiency=EvidenceSufficiencyEvaluator()
        self.questions=DiagnosticQuestionGraph()

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
                if exc.code=='DEVICE_DIAGNOSTIC_LOCKED':
                    transition_session(db,session,ReproductionEvent.DEVICE_RESOURCE_BUSY,actor=actor,reason='device_busy',payload=exc.details)
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
            session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,event_type=source_event,source='MOCK_PLATFORM',
            anchor_type=AnchorType.PRIMARY_START.value,session_relative_ms=int(relative_ms),
            timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,payload_json={'runtime_anchor':True},
        ))
        ctx=self._runtime_context(session); start=max(0,int(relative_ms)-self._profile(session).ring.pretrigger_seconds*1000)
        pre=self.platform.build_pretrigger_capture(context=ctx,start_ms=start,end_ms=int(relative_ms))
        self.capture.append_pcap(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.pcap,attempt_id=attempt.id)
        self.capture.append_log(db,session=session,start_ms=start,end_ms=int(relative_ms),data=pre.debug_log,attempt_id=attempt.id)
        self.capture.freeze(db,session=session,anchor_ms=int(relative_ms),attempt_id=attempt.id)
        transition_session(db,session,ReproductionEvent.ACTIVITY,actor=actor,reason='earliest_low_level_anchor',
                           payload={'attempt_id':attempt.id,'anchor':source_event,'relative_ms':relative_ms})
        emit_event(db,event_type=EventType.REPRODUCTION_ATTEMPT_CHANGED,case_id=session.case_id,
                   entity_type='reproduction_attempt',entity_id=attempt.id,payload={'status':attempt.status,'attempt_no':attempt.attempt_no})
        return attempt

    def end_activity_without_call(self, db: Session, *, session: ReproductionSession, attempt_id: str, relative_ms: int, end_anchor: str='FXS_ONHOOK', actor: str|None=None) -> ReproductionAttempt:
        attempt=db.get(ReproductionAttempt,attempt_id)
        if not attempt or attempt.session_id!=session.id: raise AppError('REPRODUCTION_ATTEMPT_NOT_FOUND')
        if attempt.status!=AttemptStatus.ACTIVE.value: return attempt
        attempt.status=AttemptStatus.INVALID.value; attempt.valid=False; attempt.end_anchor_type=end_anchor; attempt.end_anchor_ms=int(relative_ms); attempt.ended_at=_utcnow()
        db.add(ReproductionEventRecord(session_id=session.id,attempt_id=attempt.id,case_id=session.case_id,event_type=end_anchor,
                                       source='MOCK_PLATFORM',anchor_type=AnchorType.PRIMARY_END.value,session_relative_ms=int(relative_ms),
                                       timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,uncertainty_ms=1,payload_json={'valid_call':False}))
        self.capture.reset_after_attempt(db,session=session,invalid=True)
        transition_session(db,session,ReproductionEvent.WATCH_STARTED,actor=actor,reason='invalid_attempt_continue_watching',payload={'attempt_id':attempt.id})
        return attempt

    def bind_call(self, db: Session, *, session: ReproductionSession, relative_ms: int, external_call_ref: str|None=None, actor: str|None=None) -> ReproductionCall:
        state=ReproductionState(session.state)
        attempt=db.scalar(select(ReproductionAttempt).where(
            ReproductionAttempt.session_id==session.id,ReproductionAttempt.status==AttemptStatus.ACTIVE.value).order_by(ReproductionAttempt.attempt_no.desc()))
        if state==ReproductionState.WATCHING and not attempt:
            # Low-level anchor was missed; freeze/bind on INVITE and mark it reconstructable offline.
            attempt_no=(db.scalar(select(func.count(ReproductionAttempt.id)).where(ReproductionAttempt.session_id==session.id)) or 0)+1
            attempt=ReproductionAttempt(session_id=session.id,case_id=session.case_id,attempt_no=attempt_no,status=AttemptStatus.ACTIVE.value,
                                        start_anchor_type='SIP_INVITE_FALLBACK',start_anchor_ms=int(relative_ms),details_json={'low_level_anchor_missed':True})
            db.add(attempt); db.flush()
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
        if attempt: attempt.valid=True
        db.add(ReproductionEventRecord(session_id=session.id,attempt_id=attempt.id if attempt else None,call_id=call.id,case_id=session.case_id,
                                       event_type='SIP_INVITE',source='MOCK_PCAP',anchor_type=AnchorType.CALL_BINDING.value,
                                       session_relative_ms=int(relative_ms),timestamp_source=TimestampSource.PCAP.value,uncertainty_ms=1,
                                       payload_json={'external_call_ref':external_call_ref}))
        transition_session(db,session,ReproductionEvent.CALL_BOUND,actor=actor,reason='call_binding_event',payload={'call_id':call.id,'call_no':call.call_no})
        transition_session(db,session,ReproductionEvent.CAPTURE_STARTED,actor=actor,reason='call_scope_capture')
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
        return call

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
                                       event_type=end_anchor,source='MOCK_PLATFORM',anchor_type=AnchorType.PRIMARY_END.value,
                                       session_relative_ms=int(relative_ms),timestamp_source=TimestampSource.COLLECTOR_MONOTONIC.value,
                                       uncertainty_ms=1,payload_json={}))
        transition_session(db,session,ReproductionEvent.CALL_ENDED,actor=actor,reason='call_end_anchor',payload={'call_id':call.id})
        transition_session(db,session,ReproductionEvent.POST_CAPTURE_STARTED,actor=actor,reason='post_capture_window')
        call.status=ReproductionCallStatus.ANALYZING.value
        bind_event=db.scalar(select(ReproductionEventRecord).where(ReproductionEventRecord.call_id==call.id,ReproductionEventRecord.event_type=='SIP_INVITE').order_by(ReproductionEventRecord.session_relative_ms))
        call_start_ms=int(bind_event.session_relative_ms if bind_event and bind_event.session_relative_ms is not None else max(0,int(relative_ms)-1200))
        ctx=self._runtime_context(session); scenario=self.platform.build_call_capture(context=ctx,start_ms=call_start_ms,end_ms=int(relative_ms),call_id=call.id,profile_id=session.profile_key,signal=signal)
        pseg=self.capture.append_pcap(db,session=session,start_ms=call_start_ms,end_ms=int(relative_ms)+self._profile(session).timeouts.post_capture_seconds*1000,data=scenario.pcap,attempt_id=call.attempt_id,call_id=call.id,metadata={'mock_final_call':True,'phase':'CALL_FINAL'})
        self.capture.preserve_new_segment(db,session=session,row=pseg)
        lseg=self.capture.append_log(db,session=session,start_ms=call_start_ms,end_ms=int(relative_ms)+self._profile(session).timeouts.post_capture_seconds*1000,data=scenario.debug_log,attempt_id=call.attempt_id,call_id=call.id,metadata={'phase':'CALL_FINAL'})
        self.capture.preserve_new_segment(db,session=session,row=lseg)
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
        emit_event(db,event_type=EventType.REPRODUCTION_CALL_CHANGED,case_id=session.case_id,entity_type='reproduction_call',entity_id=call.id,
                   payload={'status':call.status,'verdict':call.verdict,'role':call.role,'sufficiency':decision.status.value})
        return call,decision

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
        snapshot=self.platform.cleanup(session_id=session.id,device=device,actions=profile.cleanup_actions)
        run.action_results_json={'actions':profile.cleanup_actions,'mock_platform':True}
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
        return session

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

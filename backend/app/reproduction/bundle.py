from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ArmValidationResult, CaptureChannelHealth, CleanupRun, ReproductionAttempt,
    ReproductionCall, ReproductionSession, VoiceRuntimeContextSnapshot, ReproductionCaptureState, ReproductionCaptureSegment, EvidenceFinalizeRun,
)


def build_reproduction_evidence_bundle(db: Session, session: ReproductionSession) -> dict:
    attempts=list(db.scalars(select(ReproductionAttempt).where(ReproductionAttempt.session_id==session.id).order_by(ReproductionAttempt.attempt_no)))
    calls=list(db.scalars(select(ReproductionCall).where(ReproductionCall.session_id==session.id).order_by(ReproductionCall.call_no)))
    health=list(db.scalars(select(CaptureChannelHealth).where(CaptureChannelHealth.session_id==session.id).order_by(CaptureChannelHealth.channel)))
    arm=list(db.scalars(select(ArmValidationResult).where(ArmValidationResult.session_id==session.id).order_by(ArmValidationResult.validation_no)))
    cleanup=list(db.scalars(select(CleanupRun).where(CleanupRun.session_id==session.id).order_by(CleanupRun.run_no)))
    context=db.scalar(select(VoiceRuntimeContextSnapshot).where(VoiceRuntimeContextSnapshot.session_id==session.id))
    capture_state=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
    segments=list(db.scalars(select(ReproductionCaptureSegment).where(ReproductionCaptureSegment.session_id==session.id).order_by(ReproductionCaptureSegment.channel,ReproductionCaptureSegment.segment_no)))
    finalizations=list(db.scalars(select(EvidenceFinalizeRun).where(EvidenceFinalizeRun.session_id==session.id).order_by(EvidenceFinalizeRun.run_no)))
    return {
        'schema_version':2,
        'session':{
            'id':session.id,'case_id':session.case_id,'device_id':session.device_id,
            'state':session.state,'profile_id':session.profile_key,'profile_version':session.profile_version,
            'profile_checksum':session.profile_checksum,'capture_stage':session.capture_stage,
            'capture_completeness':session.capture_completeness,'evidence_sufficiency':session.evidence_sufficiency,
            'cleanup_status':session.cleanup_status,'primary_target_call_id':session.primary_target_call_id,
            'terminal_reason':session.terminal_reason,
        },
        'voice_runtime_context':context.snapshot_json if context else session.voice_runtime_context_json,
        'arm_validations':[
            {'validation_no':x.validation_no,'status':x.status,'required_channels':x.required_channels_json,
             'readiness_phase':(x.observed_channels_json or {}).get('_readiness_phase'),
             'observed_channels':x.observed_channels_json,'failed_reasons':x.failed_reasons_json or []}
            for x in arm
        ],
        'capture_health':{
            x.channel:{'status':x.status,'packet_count':x.packet_count,'details':x.health_json or {}} for x in health
        },
        'capture_pipeline':{
            'state': ({'pretrigger_ms':capture_state.pretrigger_ms,'segment_ms':capture_state.segment_ms,'preserve_mode':capture_state.preserve_mode,
                       'freeze_anchor_ms':capture_state.freeze_anchor_ms,'total_bytes':capture_state.total_bytes,'finalized':capture_state.finalized,
                       'manifest':capture_state.manifest_json} if capture_state else None),
            'segments':[{'id':x.id,'channel':x.channel,'segment_no':x.segment_no,'start_ms':x.start_ms,'end_ms':x.end_ms,'size_bytes':x.size_bytes,
                         'sha256':x.sha256,'status':x.status,'frozen':x.frozen,'retained':x.retained,'retention_class':x.retention_class,
                         'evidence_id':x.evidence_id,'attempt_id':x.attempt_id,'call_id':x.call_id,'metadata':x.metadata_json or {}} for x in segments],
            'finalizations':[{'run_no':x.run_no,'status':x.status,'evidence_ids':x.evidence_ids_json or [],'manifest_object_key':x.manifest_object_key,
                              'manifest_sha256':x.manifest_sha256,'error_code':x.error_code} for x in finalizations],
        },
        'attempts':[
            {'id':x.id,'attempt_no':x.attempt_no,'status':x.status,'valid':x.valid,
             'start_anchor_type':x.start_anchor_type,'start_anchor_ms':x.start_anchor_ms,
             'end_anchor_type':x.end_anchor_type,'end_anchor_ms':x.end_anchor_ms,'details':x.details_json or {}}
            for x in attempts
        ],
        'calls':[
            {'id':x.id,'call_no':x.call_no,'attempt_id':x.attempt_id,'status':x.status,
             'verdict':x.verdict,'role':x.role,'quick_analysis':x.quick_analysis_json or {}}
            for x in calls
        ],
        'cleanup_runs':[
            {'run_no':x.run_no,'status':x.status,'validation':x.validation_json or {},'error_code':x.error_code} for x in cleanup
        ],
    }

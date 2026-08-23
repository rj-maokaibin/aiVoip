from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    ArmValidationStatus, CaptureChannel, ChannelHealth, CleanupRunStatus, CleanupStatus,
    EventType, EvidenceSufficiency,
)
from app.db.models import ArmValidationResult, CaptureChannelHealth, CleanupRun, ReproductionSession
from app.reproduction.profile import ReproductionProfileDefinition
from app.services.events import emit_event


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ArmBarrierDecision:
    ready: bool
    status: ArmValidationStatus
    failed_reasons: tuple[str, ...]
    observed: dict
    readiness_phase: str = 'DATA_PLANE_VERIFIED'


class ArmReadinessBarrier:
    @staticmethod
    def _validation_no(db: Session, session_id: str) -> int:
        return (db.scalar(select(func.count(ArmValidationResult.id)).where(
            ArmValidationResult.session_id == session_id)) or 0) + 1

    @staticmethod
    def _channel_ready(channel: CaptureChannel, observed: dict, profile: ReproductionProfileDefinition) -> tuple[bool,str|None]:
        data=observed.get(channel.value) or {}
        activity_gated=(profile.arm_barrier.readiness_mode == 'ACTIVITY_GATED')
        path_ready=(activity_gated and channel in {CaptureChannel.PCM_RX,CaptureChannel.PCM_TX}
                    and data.get('configured') is True
                    and data.get('status') in {ChannelHealth.STARTING.value,ChannelHealth.HEALTHY.value})
        if data.get('status') != ChannelHealth.HEALTHY.value and not path_ready:
            return False, f'{channel.value}_NOT_HEALTHY'
        require_adv=profile.arm_barrier.require_advancing
        if channel == CaptureChannel.PCAP:
            # Capture V2 Stage-1 is a pre-OFFHOOK path-readiness contract.  A real
            # business packet/rotated file may not exist yet, so an already-persisted
            # Phase-D CAPTURE_PATH_READY proof can satisfy PCAP readiness without
            # pretending that a PCAP header was observed. Legacy/non-activity-gated
            # paths still require the historical header proof.
            capture_path_ready=(activity_gated and data.get('capture_path_ready') is True)
            if not data.get('pcap_header_valid',False) and not capture_path_ready:
                return False,'PCAP_HEADER_INVALID'
            if int(data.get('packet_count',0)) < profile.arm_barrier.min_pcap_packets: return False,'PCAP_PACKET_COUNT_LOW'
            if require_adv and not data.get('advancing',False): return False,'PCAP_NOT_ADVANCING'
        elif channel in {CaptureChannel.PCM_RX,CaptureChannel.PCM_TX}:
            if path_ready:
                return True,None
            if int(data.get('packet_count',0)) < profile.arm_barrier.min_pcm_packets: return False,f'{channel.value}_PACKET_COUNT_LOW'
            if require_adv and not data.get('advancing',False): return False,f'{channel.value}_NOT_ADVANCING'
        elif channel == CaptureChannel.DEBUG:
            if not data.get('enabled',False): return False,'DEBUG_NOT_ENABLED'
            if not data.get('heartbeat',False): return False,'DEBUG_READER_NOT_HEALTHY'
        return True,None

    @classmethod
    def evaluate(cls, profile: ReproductionProfileDefinition, observed: dict, required_channels: list[CaptureChannel]) -> ArmBarrierDecision:
        failures=[]
        for channel in required_channels:
            ok,reason=cls._channel_ready(channel,observed,profile)
            if not ok and reason: failures.append(reason)
        if not failures:
            pending=(profile.arm_barrier.readiness_mode == 'ACTIVITY_GATED' and any(
                (observed.get(ch.value) or {}).get('verification_pending')
                for ch in (CaptureChannel.PCM_RX,CaptureChannel.PCM_TX)
            ))
            return ArmBarrierDecision(
                True,
                ArmValidationStatus.PARTIAL if pending else ArmValidationStatus.PASSED,
                (),observed,
                'CAPTURE_PATH_READY' if pending else 'DATA_PLANE_VERIFIED',
            )
        if profile.allow_partial_capability_downgrade:
            pcap_ok,_=cls._channel_ready(CaptureChannel.PCAP,observed,profile)
            if pcap_ok:
                return ArmBarrierDecision(True,ArmValidationStatus.PARTIAL,tuple(failures),observed,'CAPTURE_PATH_DEGRADED')
        return ArmBarrierDecision(False,ArmValidationStatus.FAILED,tuple(failures),observed,'NOT_READY')

    @classmethod
    def persist(cls, db: Session, *, session: ReproductionSession, profile: ReproductionProfileDefinition, observed: dict, required_channels: list[CaptureChannel]) -> ArmBarrierDecision:
        decision=cls.evaluate(profile,observed,required_channels)
        validation_no=cls._validation_no(db, session.id)
        now=_utcnow()
        observed_snapshot={**observed, '_readiness_phase': decision.readiness_phase}
        db.add(ArmValidationResult(
            session_id=session.id, validation_no=validation_no, status=decision.status.value,
            required_channels_json=[x.value for x in required_channels], observed_channels_json=observed_snapshot,
            failed_reasons_json=list(decision.failed_reasons), started_at=now, finished_at=now,
        ))
        for channel in CaptureChannel:
            data=observed.get(channel.value) or {}
            row=db.scalar(select(CaptureChannelHealth).where(
                CaptureChannelHealth.session_id==session.id, CaptureChannelHealth.channel==channel.value))
            if not row:
                row=CaptureChannelHealth(session_id=session.id,channel=channel.value)
                db.add(row)
            row.status=str(data.get('status') or ChannelHealth.UNKNOWN.value)
            row.packet_count=int(data.get('packet_count',0) or 0)
            row.last_observed_at=now if data else None
            row.health_json=data
        db.flush()
        emit_event(db,event_type=EventType.REPRODUCTION_ARM_VALIDATED,case_id=session.case_id,
                   entity_type='reproduction_session',entity_id=session.id,
                   payload={'status':decision.status.value,'ready':decision.ready,
                            'readiness_phase':decision.readiness_phase,
                            'failed_reasons':list(decision.failed_reasons)})
        return decision

    @classmethod
    def persist_activity_data_plane_validation(
        cls,
        db: Session,
        *,
        session: ReproductionSession,
    ) -> ArmBarrierDecision:
        """Persist the second ACTIVITY_GATED readiness phase as Arm Evidence.

        Command/path readiness is recorded during ARM. This method records whether
        the first real business activity actually produced both required PCM
        directions, so CAPTURE_PATH_READY is never mistaken for data-plane proof.
        """
        rows = {row.channel: row for row in db.scalars(select(CaptureChannelHealth).where(
            CaptureChannelHealth.session_id == session.id,
            CaptureChannelHealth.channel.in_([
                CaptureChannel.PCM_RX.value, CaptureChannel.PCM_TX.value,
            ]),
        ))}
        observed: dict[str, dict] = {}
        failures: list[str] = []
        for channel in (CaptureChannel.PCM_RX, CaptureChannel.PCM_TX):
            row = rows.get(channel.value)
            packet_count = int((row.packet_count if row else 0) or 0)
            verified = packet_count > 0
            if not verified:
                failures.append(f'{channel.value}_NOT_VERIFIED')
                if row is not None:
                    row.status = ChannelHealth.DEGRADED.value
            details = dict((row.health_json if row else {}) or {})
            details.update({
                'packet_count': packet_count,
                'advancing': verified,
                'verification_pending': False,
                'readiness_phase': 'DATA_PLANE_VERIFIED' if verified else 'CAPTURE_PATH_DEGRADED',
            })
            if row is not None:
                row.health_json = details
            observed[channel.value] = details

        verified = not failures
        phase = 'DATA_PLANE_VERIFIED' if verified else 'CAPTURE_PATH_DEGRADED'
        status = ArmValidationStatus.PASSED if verified else ArmValidationStatus.PARTIAL
        observed['_readiness_phase'] = phase
        now = _utcnow()
        db.add(ArmValidationResult(
            session_id=session.id,
            validation_no=cls._validation_no(db, session.id),
            status=status.value,
            required_channels_json=[CaptureChannel.PCM_RX.value, CaptureChannel.PCM_TX.value],
            observed_channels_json=observed,
            failed_reasons_json=failures,
            started_at=now,
            finished_at=now,
        ))
        if failures:
            session.evidence_sufficiency = EvidenceSufficiency.INSUFFICIENT_CAPTURE_RECOVERY.value
        decision = ArmBarrierDecision(verified, status, tuple(failures), observed, phase)
        emit_event(
            db,
            event_type=EventType.REPRODUCTION_ARM_VALIDATED,
            case_id=session.case_id,
            entity_type='reproduction_session',
            entity_id=session.id,
            payload={
                'status': status.value,
                'ready': verified,
                'readiness_phase': phase,
                'failed_reasons': failures,
            },
        )
        return decision


@dataclass(frozen=True)
class CleanupBarrierDecision:
    status: CleanupRunStatus
    cleanup_status: CleanupStatus
    verified: bool
    failures: tuple[str, ...]
    validation: dict


class CleanupReadinessBarrier:
    @staticmethod
    def evaluate(snapshot: dict) -> CleanupBarrierDecision:
        reverse=snapshot.get('reverse_validation') or {}
        final=snapshot.get('final') or {}
        failures=[]
        for channel in (CaptureChannel.PCM_RX,CaptureChannel.PCM_TX):
            data=reverse.get(channel.value) or {}
            if data.get('enabled') or data.get('advancing') or not data.get('quiet_verified',False):
                failures.append(f'{channel.value}_STILL_ACTIVE')
        debug=reverse.get(CaptureChannel.DEBUG.value) or {}
        if debug.get('enabled') or not debug.get('off_verified',False):
            failures.append('DEBUG_STILL_ACTIVE')
        pcap=final.get(CaptureChannel.PCAP.value) or {}
        if pcap.get('enabled') or not pcap.get('closed_verified',False):
            failures.append('PCAP_STILL_ACTIVE')
        if failures:
            return CleanupBarrierDecision(CleanupRunStatus.FAILED,CleanupStatus.CLEANUP_FAILED,False,tuple(failures),snapshot)
        return CleanupBarrierDecision(CleanupRunStatus.VERIFIED,CleanupStatus.CLEANUP_VERIFIED,True,(),snapshot)

    @classmethod
    def persist(cls, db: Session, *, session: ReproductionSession, run: CleanupRun, snapshot: dict) -> CleanupBarrierDecision:
        decision=cls.evaluate(snapshot)
        run.status=decision.status.value
        run.validation_json=decision.validation
        run.error_code=';'.join(decision.failures) if decision.failures else None
        run.finished_at=_utcnow()
        session.cleanup_status=decision.cleanup_status.value
        db.flush()
        emit_event(db,event_type=EventType.REPRODUCTION_CLEANUP_VALIDATED,case_id=session.case_id,
                   entity_type='reproduction_session',entity_id=session.id,
                   payload={'status':decision.cleanup_status.value,'verified':decision.verified,'failures':list(decision.failures)})
        return decision

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa: F401
from app.db.base import Base
from app.capture_v2.db_models import (
    AttemptDataPlaneVerification, CaptureAttempt, CaptureEpoch, CaptureEvent,
    CaptureGap, CaptureSegment, CaptureSession, CoverageInterval, CoverageTrack,
    CoverageWindow, EvidenceAsset, QualitySnapshot, ReadinessSnapshot, SignalAvailability,
)
from app.capture_v2.enums import CaptureHealth, CaptureSessionState, CoverageIntervalType
from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.fxs.sanitizer import RawFxsEvent
from app.capture_v2.quality.signals import SignalEvidence
from app.capture_v2.readiness.stage1 import CapturePathChecks
from app.capture_v2.runtime_coordinator import CaptureV2RuntimeCoordinator

T0 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def factory():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    tables = [
        CaptureSession.__table__, CaptureEpoch.__table__, CaptureGap.__table__, CaptureSegment.__table__,
        CaptureEvent.__table__, ReadinessSnapshot.__table__, CaptureAttempt.__table__,
        AttemptDataPlaneVerification.__table__, CoverageWindow.__table__, CoverageTrack.__table__,
        CoverageInterval.__table__, QualitySnapshot.__table__, SignalAvailability.__table__,
        EvidenceAsset.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    F = sessionmaker(bind=engine, expire_on_commit=False)
    effective = {
        'resolved': {
            'fxs': {
                'hook_glitch_max_ms': 100,
                'post_onhook_rebound_window_ms': 500,
                'stable_offhook_confirm_ms': 100,
                'hook_flash_min_ms': 100,
                'hook_flash_max_ms': 1000,
            },
            'readiness': {
                'pcm_readiness_timeout_seconds': 1,
                'sip_expectation_timeout_seconds': 3,
                'rtp_expectation_timeout_seconds': 3,
                'pcm_media_expectation_timeout_seconds': 2,
            },
            'coverage': {'pre_trigger_seconds': 0, 'post_trigger_seconds': 0},
            'channels': {'pcap': 'REQUIRED', 'fxs': 'REQUIRED'},
        }
    }
    with F() as db, db.begin():
        db.add(CaptureSession(
            id='S', reproduction_session_id='R', device_id='D',
            state=CaptureSessionState.PREPARING.value, health_status=CaptureHealth.HEALTHY.value,
            capture_profile_id='p', capture_profile_version='1',
            platform_profile_id='mt7621', platform_profile_version='1', effective_profile=effective,
        ))
    return F, effective


def ready_checks():
    return CapturePathChecks(
        lease_active=True, exactly_one_producer=True, voice_context_ready=True,
        pcap_ready=True, fxs_ready=True, pcm_control_ready=True,
        server_store_ready=True, transfer_ready=True, storage_guard_ready=True,
        watchdog_ready=True,
    )


def test_runtime_coordinator_sequences_watching_target_finalize_cleanup():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    assert rt.arm_to_watching(ready_checks()).value == 'READY'
    assert rt.sessions.state('S') == CaptureSessionState.WATCHING

    # OFFHOOK candidate -> stable confirmation -> DATA_PLANE_VERIFYING.
    ids = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))
    assert len(ids) == 1
    aid = ids[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    with F() as db:
        assert db.get(CaptureAttempt, aid).state == 'DATA_PLANE_VERIFYING'

    # Event-conditioned SIP expectation and actual source-time call binding.
    rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0 + timedelta(seconds=1))
    bind = rt.bind_signal(
        source_ts=T0 + timedelta(seconds=1.2), binding_event='SIP_INVITE', call_ref='CALL-1'
    )
    assert bind.capture_attempt_id == aid
    rt.observe_channel(
        capture_attempt_id=aid, channel='SIP', source_ts=T0 + timedelta(seconds=1.2)
    )

    rt.mark_target_confirmed(
        capture_attempt_id=aid, source_ts=T0 + timedelta(seconds=1.5), reason='TARGET_AUDIO_ANOMALY'
    )
    assert rt.sessions.state('S') == CaptureSessionState.TARGET_CONFIRMED

    # During-call ONHOOK remains pending until hook-flash window expires.
    rt.mark_call_ended(capture_attempt_id=aid, source_ts=T0 + timedelta(seconds=2))
    rt.ingest_fxs(RawFxsEvent(T0 + timedelta(seconds=2), 'ONHOOK'), call_active=True)
    rt.tick(now=T0 + timedelta(seconds=3.1))
    assert rt.sessions.state('S') == CaptureSessionState.POST_TARGET_OBSERVATION
    with F() as db:
        attempt = db.get(CaptureAttempt, aid)
        assert attempt.state == 'ENDED'
        start = attempt.confirmed_start_source_ts
        end = attempt.ended_source_ts
        db.add(CaptureEpoch(
            id='E', capture_session_id='S', device_id='D', epoch_index=1,
            epoch_token='CAP_E', lease_epoch_started=1, state='ENDED',
            started_at=T0 - timedelta(seconds=1), ended_at=T0 + timedelta(seconds=4),
            packets_dropped_kernel=0,
        ))
        db.commit()

    rt.begin_evidence_drain(source_ts=T0 + timedelta(seconds=4))
    # C finalizer owns this timestamp; software runtime may enter Coverage only
    # after durable evidence (or an explicit PARTIAL decision).
    with F() as db, db.begin():
        db.get(CaptureSession, 'S').evidence_durable_at = T0 + timedelta(seconds=4.05)
    rt.begin_coverage_finalizing(source_ts=T0 + timedelta(seconds=4.1))
    result = rt.finalize_attempt(
        capture_attempt_id=aid, call_ref='CALL-1',
        channel_evidence={
            'FXS': [EvidenceInterval(start, end, CoverageIntervalType.COVERED, 'FXS_EVENT', aid)],
        },
        channel_applicability={},
        signals=[SignalEvidence(channel='SIP', expected=True, captured=True, usable=True)],
        required_channels_for_diagnosis=('SIP',), independent_support_count=2,
    )
    assert result.coverage_status == 'COMPLETE'
    assert result.quality['capture_completeness'] == 'COMPLETE'
    with F() as db:
        assert db.get(CaptureAttempt, aid).state == 'EVALUATED'
        q = db.get(QualitySnapshot, result.quality_snapshot_id)
        assert q.coverage_window_id == result.coverage_window_id

    rt.begin_cleanup(source_ts=T0 + timedelta(seconds=5))
    # Production completion reads the persisted cleanup ledger, never a free boolean.
    with F() as db, db.begin():
        db.get(CaptureSession, 'S').cleanup_status = 'VERIFIED'
    rt.complete_from_cleanup(source_ts=T0 + timedelta(seconds=5.1))
    assert rt.sessions.state('S') == CaptureSessionState.COMPLETED


def test_late_sip_fallback_then_late_offhook_refines_anchor_by_source_time():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    result = rt.bind_signal(
        source_ts=T0 + timedelta(seconds=2), binding_event='SIP_INVITE', call_ref='CALL-X'
    )
    aid = result.capture_attempt_id
    assert result.created_fallback is True
    with F() as db:
        row = db.get(CaptureAttempt, aid)
        assert row.classification == 'FALLBACK_ANCHORED'
        assert row.candidate_start_source_ts.replace(tzinfo=timezone.utc) == T0 + timedelta(seconds=2)

    # Collector receives the OFFHOOK later, but its embedded DUT Source Time is 1s.
    ids = rt.ingest_fxs(RawFxsEvent(T0 + timedelta(seconds=1), 'OFFHOOK'))
    assert aid in ids
    rt.tick(now=T0 + timedelta(seconds=1.2))
    with F() as db:
        row = db.get(CaptureAttempt, aid)
        ts = row.candidate_start_source_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert ts == T0 + timedelta(seconds=1)
        assert (row.metadata_json or {}).get('fxs_corroborated') is True
        assert (row.metadata_json or {}).get('anchor_revision_history')


def test_target_confirmed_rejects_new_attempt_semantics_but_keeps_raw_event():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    aid = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    rt.mark_target_confirmed(capture_attempt_id=aid, source_ts=T0 + timedelta(seconds=1), reason='TARGET')
    # End current target attempt without active call.
    rt.ingest_fxs(RawFxsEvent(T0 + timedelta(seconds=2), 'ONHOOK'), call_active=False)
    assert rt.sessions.state('S') == CaptureSessionState.POST_TARGET_OBSERVATION

    # A later OFFHOOK is not accepted as a new Attempt, but raw evidence is retained.
    ids = rt.ingest_fxs(RawFxsEvent(T0 + timedelta(seconds=3), 'OFFHOOK'))
    assert ids == ()
    with F() as db:
        attempts = list(db.scalars(select(CaptureAttempt).where(CaptureAttempt.capture_session_id == 'S')))
        raws = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == 'S', CaptureEvent.event_type == 'FXS_RAW_OFFHOOK'
        )))
        rejected = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == 'S', CaptureEvent.event_type == 'NEW_ATTEMPT_REJECTED'
        )))
        assert len(attempts) == 1
        assert len(raws) == 2
        assert len(rejected) == 1


def test_binding_update_call_state_can_confirm_provisional_after_sqlite_roundtrip():
    from app.capture_v2.timeline.binding import EventualBindingService
    from app.capture_v2.db_models import CaptureAttempt
    from app.capture_v2.enums import CaptureAttemptState
    from app.core.ids import new_id

    F, _effective = factory()
    with F() as db:
        with db.begin():
            row = CaptureAttempt(
                id=new_id(), capture_session_id='S', attempt_no=1,
                state=CaptureAttemptState.PROVISIONAL.value,
                candidate_start_source_ts=T0,
                metadata_json={
                    'call': {
                        'call_ref': 'CALL-P', 'binding_event': 'SIP_INVITE',
                        'binding_source_ts': T0.isoformat(), 'state': 'CONFIRMED', 'history': [],
                    }
                },
            )
            db.add(row)
            aid = row.id
    EventualBindingService(F).update_call_state(
        capture_attempt_id=aid, state='SIGNALING', source_ts=T0 + timedelta(seconds=1)
    )
    with F() as db:
        row = db.get(CaptureAttempt, aid)
        assert row.state == CaptureAttemptState.CONFIRMED.value
        assert row.confirmation_source == 'CALL_SIGNALING'
        assert (row.metadata_json or {}).get('call', {}).get('state') == 'SIGNALING'


def test_data_plane_late_processing_can_correct_missing_when_source_time_was_in_deadline():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    aid = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0)
    missing = rt.stack.d.expire_expectations(capture_attempt_id=aid, now=T0 + timedelta(seconds=4))
    assert missing == ('SIP',)
    # Analysis/transfer is late, but packet Source Time is inside the 3s SIP expectation.
    rt.observe_channel(capture_attempt_id=aid, channel='SIP', source_ts=T0 + timedelta(seconds=2.5))
    assert rt.stack.d.data_plane.snapshot(aid)['SIP'] == 'VERIFIED'


def test_data_plane_source_time_after_deadline_stays_missing():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    aid = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0)
    rt.stack.d.expire_expectations(capture_attempt_id=aid, now=T0 + timedelta(seconds=4))
    rt.observe_channel(capture_attempt_id=aid, channel='SIP', source_ts=T0 + timedelta(seconds=3.5))
    assert rt.stack.d.data_plane.snapshot(aid)['SIP'] == 'MISSING'


def test_channel_expectation_conflicting_redefinition_fails_closed():
    F, effective = factory()
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    aid = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    first = rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0)
    assert rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0) == first
    import pytest
    from app.capture_v2.errors import CaptureV2Error
    with pytest.raises(CaptureV2Error) as exc:
        rt.expect_channel(capture_attempt_id=aid, channel='SIP', trigger_ts=T0 + timedelta(seconds=1))
    assert exc.value.code == 'CHANNEL_EXPECTATION_CONFLICT'


def test_post_target_and_evidence_finalize_timers_advance_automatically_and_fail_partial_not_complete():
    F, effective = factory()
    effective['resolved']['lifecycle'] = {
        'post_target_seconds': 2,
        'evidence_finalize_timeout_seconds': 3,
    }
    with F() as db, db.begin():
        db.get(CaptureSession, 'S').effective_profile = effective
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    aid = rt.ingest_fxs(RawFxsEvent(T0, 'OFFHOOK'))[0]
    rt.tick(now=T0 + timedelta(milliseconds=150))
    rt.mark_target_confirmed(capture_attempt_id=aid, source_ts=T0 + timedelta(seconds=1), reason='TARGET')
    rt.ingest_fxs(RawFxsEvent(T0 + timedelta(seconds=2), 'ONHOOK'))
    assert rt.sessions.state('S') == CaptureSessionState.POST_TARGET_OBSERVATION
    rt.tick(now=T0 + timedelta(seconds=3.9))
    assert rt.sessions.state('S') == CaptureSessionState.POST_TARGET_OBSERVATION
    rt.tick(now=T0 + timedelta(seconds=4.1))
    assert rt.sessions.state('S') == CaptureSessionState.EVIDENCE_DRAINING
    rt.tick(now=T0 + timedelta(seconds=7.2))
    assert rt.sessions.state('S') == CaptureSessionState.COVERAGE_FINALIZING
    with F() as db:
        events = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == 'S', CaptureEvent.event_type == 'EVIDENCE_EXPLICIT_PARTIAL'
        )))
        assert len(events) == 1
        assert events[0].payload['reason'] == 'EVIDENCE_FINALIZE_TIMEOUT'


def test_evidence_durable_causes_automatic_coverage_transition_on_tick():
    F, effective = factory()
    effective['resolved']['lifecycle'] = {'post_target_seconds': 10, 'evidence_finalize_timeout_seconds': 120}
    rt = CaptureV2RuntimeCoordinator(session_factory=F, capture_session_id='S', effective_profile=effective)
    rt.arm_to_watching(ready_checks())
    rt.begin_evidence_drain(source_ts=T0)
    with F() as db, db.begin():
        db.get(CaptureSession, 'S').evidence_durable_at = T0 + timedelta(seconds=1)
    rt.tick(now=T0 + timedelta(seconds=1.1))
    assert rt.sessions.state('S') == CaptureSessionState.COVERAGE_FINALIZING

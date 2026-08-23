from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.capture_v2.attempt_flow import CaptureAttemptFlow
from app.capture_v2.db_models import CaptureAttempt, CaptureEvent, CaptureSession, QualitySnapshot
from app.capture_v2.enums import CaptureAttemptState, CaptureSessionState, ReadinessStatus
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.fxs.sanitizer import RawFxsEvent
from app.capture_v2.readiness.stage1 import CapturePathChecks
from app.capture_v2.session_flow import CaptureSessionFlow
from app.capture_v2.software_stack import CaptureV2SoftwareStack
from app.capture_v2.timeline.binding import BindingResult, EventualBindingService
from app.capture_v2.timeline.source_time import normalize_utc


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FinalizedAttemptResult:
    capture_attempt_id: str
    coverage_window_id: str
    coverage_status: str
    quality_snapshot_id: str
    quality: dict


class CaptureV2RuntimeCoordinator:
    """Pure/stateful V2.1 D/E/F runtime orchestration.

    Device I/O is intentionally injected elsewhere. This class owns semantic state
    progression so real-gate code only feeds raw/source-timestamped observations and
    invokes cleanup/finalizer actions; it must not invent alternate business rules.
    """

    def __init__(self, *, session_factory, capture_session_id: str, effective_profile: dict):
        self.session_factory = session_factory
        self.capture_session_id = capture_session_id
        self.stack = CaptureV2SoftwareStack(
            session_factory=session_factory,
            capture_session_id=capture_session_id,
            effective_profile=effective_profile,
        )
        self.sessions = CaptureSessionFlow(session_factory)
        self.attempt_flow = CaptureAttemptFlow(session_factory)
        self.binding = EventualBindingService(session_factory)
        resolved = dict(effective_profile.get("resolved") or effective_profile)
        self.lifecycle = dict(resolved.get("lifecycle") or {})

    def arm_to_watching(self, checks: CapturePathChecks) -> ReadinessStatus:
        decision = self.stack.d.evaluate_stage1(checks)
        if decision.status != ReadinessStatus.READY:
            return decision.status
        state = self.sessions.state(self.capture_session_id)
        if state == CaptureSessionState.PREPARING:
            # ReadinessRepository owns PREPARING->CAPTURE_PATH_READY.
            state = self.sessions.state(self.capture_session_id)
        if state == CaptureSessionState.CAPTURE_PATH_READY:
            self.sessions.transition(
                self.capture_session_id, target=CaptureSessionState.WATCHING,
                reason="CAPTURE_PATH_READY_CONFIRMED",
            )
        elif state != CaptureSessionState.WATCHING:
            raise CaptureV2Error(
                "CAPTURE_SESSION_NOT_WATCHABLE", details={"state": state.value}
            )
        return decision.status

    def _active_attempt(self) -> CaptureAttempt | None:
        return self.attempt_flow.active(self.capture_session_id)

    def _audit_rejected_raw(self, event: RawFxsEvent, reason: str) -> None:
        self.stack.d.attempts.append_raw_event(
            capture_session_id=self.capture_session_id, source_ts=event.source_ts,
            event=event.event, digit=event.digit, line=event.line,
        )
        with self.session_factory() as db:
            with db.begin():
                db.add(CaptureEvent(
                    capture_session_id=self.capture_session_id,
                    entity_type="CAPTURE_SESSION", entity_id=self.capture_session_id,
                    event_type="NEW_ATTEMPT_REJECTED", source_ts=event.source_ts,
                    payload={"reason": reason, "raw_event": event.event, "digit": event.digit},
                ))

    def ingest_fxs(self, event: RawFxsEvent, *, call_active: bool = False) -> tuple[str, ...]:
        state = self.sessions.state(self.capture_session_id)
        active = self._active_attempt()
        if event.event.upper() == "OFFHOOK" and active is None and state != CaptureSessionState.WATCHING:
            self._audit_rejected_raw(event, "SESSION_NOT_ACCEPTING_NEW_ATTEMPT")
            return ()
        if state not in (
            CaptureSessionState.WATCHING,
            CaptureSessionState.TARGET_CONFIRMED,
        ) and active is None:
            self._audit_rejected_raw(event, "SESSION_NOT_LISTENING")
            return ()

        ids = self.stack.d.ingest_fxs(event, call_active=call_active)
        for attempt_id in ids:
            self._promote_confirmed_attempt(attempt_id, source_ts=event.source_ts)
        self._advance_post_target_if_ended(source_ts=event.source_ts)
        return ids

    def tick(self, *, now: datetime) -> tuple[str, ...]:
        now = normalize_utc(now)
        ids = list(self.stack.d.tick_hook_stability(now=now))
        ids.extend(self.stack.d.flush_pending_onhook(now=now))
        for attempt_id in dict.fromkeys(ids):
            self._promote_confirmed_attempt(attempt_id, source_ts=now)
        self._advance_post_target_if_ended(source_ts=now)
        self._advance_session_timers(now=now)
        return tuple(dict.fromkeys(ids))

    def _latest_session_event_ts(self, event_type: str) -> datetime | None:
        with self.session_factory() as db:
            row = db.scalar(select(CaptureEvent).where(
                CaptureEvent.capture_session_id == self.capture_session_id,
                CaptureEvent.event_type == event_type,
            ).order_by(CaptureEvent.recorded_at.desc()).limit(1))
            return normalize_utc(row.source_ts) if row is not None and row.source_ts is not None else None

    def _advance_session_timers(self, *, now: datetime) -> None:
        state = self.sessions.state(self.capture_session_id)
        if state == CaptureSessionState.POST_TARGET_OBSERVATION:
            entered = self._latest_session_event_ts("SESSION_POST_TARGET_OBSERVATION")
            tail = float(self.lifecycle.get("post_target_seconds", 10.0))
            if entered is not None and (now - entered).total_seconds() >= tail:
                self.begin_evidence_drain(source_ts=now, reason="POST_TARGET_WINDOW_COMPLETE")
                state = self.sessions.state(self.capture_session_id)
        if state == CaptureSessionState.EVIDENCE_DRAINING:
            with self.session_factory() as db:
                session = db.get(CaptureSession, self.capture_session_id)
                durable = session is not None and session.evidence_durable_at is not None
            if durable:
                self.begin_coverage_finalizing(source_ts=now)
                return
            entered = self._latest_session_event_ts("SESSION_EVIDENCE_DRAINING")
            timeout = float(self.lifecycle.get("evidence_finalize_timeout_seconds", 120.0))
            if entered is not None and (now - entered).total_seconds() >= timeout:
                self.begin_coverage_finalizing(
                    source_ts=now, explicit_partial=True,
                    partial_reason="EVIDENCE_FINALIZE_TIMEOUT",
                )

    def _promote_confirmed_attempt(self, attempt_id: str, *, source_ts: datetime) -> None:
        with self.session_factory() as db:
            row = db.get(CaptureAttempt, attempt_id)
            if row is None:
                return
            state = row.state
        if state == CaptureAttemptState.CONFIRMED.value:
            self.attempt_flow.begin_data_plane(attempt_id, source_ts=source_ts)

    def bind_signal(
        self, *, source_ts: datetime, binding_event: str, call_ref: str | None = None,
        allow_fallback_create: bool = True, details: dict | None = None,
    ) -> BindingResult:
        # If raw OFFHOOK created an in-memory provisional candidate, business
        # evidence confirms it before DB binding. If no provisional exists the
        # EventualBindingService may create a fallback-anchored Attempt.
        for attempt_id in self.stack.d.confirm_business_evidence(
            source_ts=source_ts, source=binding_event
        ):
            self._promote_confirmed_attempt(attempt_id, source_ts=source_ts)
        result = self.binding.bind(
            capture_session_id=self.capture_session_id, source_ts=source_ts,
            binding_event=binding_event, call_ref=call_ref,
            allow_fallback_create=allow_fallback_create, details=details,
        )
        self._promote_confirmed_attempt(result.capture_attempt_id, source_ts=source_ts)
        if binding_event == "SIP_INVITE":
            self.binding.update_call_state(
                capture_attempt_id=result.capture_attempt_id, state="SIGNALING",
                source_ts=source_ts, details=details,
            )
        elif binding_event in ("RTP_STREAM_START", "MEDIA_ACTIVE"):
            self.binding.update_call_state(
                capture_attempt_id=result.capture_attempt_id, state="MEDIA_ACTIVE",
                source_ts=source_ts, details=details,
            )
        return result

    def expect_channel(self, *, capture_attempt_id: str, channel: str,
                       trigger_ts: datetime, applicable: bool = True,
                       details: dict | None = None) -> str:
        return self.stack.d.create_channel_expectation(
            capture_attempt_id=capture_attempt_id, channel=channel,
            trigger_ts=trigger_ts, applicable=applicable, details=details,
        )

    def observe_channel(self, *, capture_attempt_id: str, channel: str,
                        source_ts: datetime, details: dict | None = None) -> None:
        self.stack.d.observe_channel(
            capture_attempt_id=capture_attempt_id, channel=channel,
            source_ts=source_ts, details=details,
        )

    def mark_call_ended(self, *, capture_attempt_id: str, source_ts: datetime,
                        details: dict | None = None) -> None:
        self.binding.update_call_state(
            capture_attempt_id=capture_attempt_id, state="ENDED",
            source_ts=source_ts, details=details,
        )

    def mark_target_confirmed(self, *, capture_attempt_id: str, source_ts: datetime,
                              reason: str, details: dict | None = None) -> None:
        with self.session_factory() as db:
            attempt = db.get(CaptureAttempt, capture_attempt_id)
            if attempt is None or attempt.state == CaptureAttemptState.CLASSIFIED_GLITCH.value:
                raise CaptureV2Error("TARGET_ATTEMPT_INVALID")
        self.sessions.transition(
            self.capture_session_id, target=CaptureSessionState.TARGET_CONFIRMED,
            source_ts=source_ts, reason=reason,
            payload={"capture_attempt_id": capture_attempt_id, **(details or {})},
        )

    def _target_attempt_id(self) -> str | None:
        with self.session_factory() as db:
            rows = list(db.scalars(select(CaptureEvent).where(
                CaptureEvent.capture_session_id == self.capture_session_id,
                CaptureEvent.event_type == "SESSION_TARGET_CONFIRMED",
            ).order_by(CaptureEvent.recorded_at.desc()).limit(1)))
            if not rows:
                return None
            return (rows[0].payload or {}).get("capture_attempt_id")

    def _advance_post_target_if_ended(self, *, source_ts: datetime) -> None:
        if self.sessions.state(self.capture_session_id) != CaptureSessionState.TARGET_CONFIRMED:
            return
        target_id = self._target_attempt_id()
        if not target_id:
            return
        with self.session_factory() as db:
            row = db.get(CaptureAttempt, target_id)
            ended = row is not None and row.state == CaptureAttemptState.ENDED.value
        if ended:
            self.sessions.transition(
                self.capture_session_id,
                target=CaptureSessionState.POST_TARGET_OBSERVATION,
                source_ts=source_ts, reason="TARGET_ATTEMPT_ENDED",
                payload={"capture_attempt_id": target_id},
            )

    def begin_evidence_drain(self, *, source_ts: datetime | None = None,
                             reason: str = "POST_OBSERVATION_COMPLETE") -> None:
        state = self.sessions.state(self.capture_session_id)
        if state not in (CaptureSessionState.POST_TARGET_OBSERVATION, CaptureSessionState.WATCHING):
            if state == CaptureSessionState.EVIDENCE_DRAINING:
                return
            raise CaptureV2Error("EVIDENCE_DRAIN_NOT_ALLOWED", details={"state": state.value})
        self.sessions.transition(
            self.capture_session_id, target=CaptureSessionState.EVIDENCE_DRAINING,
            source_ts=source_ts, reason=reason,
        )

    def begin_coverage_finalizing(self, *, source_ts: datetime | None = None,
                                  explicit_partial: bool = False,
                                  partial_reason: str | None = None) -> None:
        with self.session_factory() as db:
            row = db.get(CaptureSession, self.capture_session_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
            durable = row.evidence_durable_at is not None
        if not durable and not explicit_partial:
            raise CaptureV2Error("EVIDENCE_NOT_DURABLE")
        if explicit_partial and not partial_reason:
            raise CaptureV2Error("EXPLICIT_PARTIAL_REASON_REQUIRED")
        if explicit_partial:
            with self.session_factory() as db:
                with db.begin():
                    db.add(CaptureEvent(
                        capture_session_id=self.capture_session_id,
                        entity_type="CAPTURE_SESSION", entity_id=self.capture_session_id,
                        event_type="EVIDENCE_EXPLICIT_PARTIAL", source_ts=source_ts or utcnow(),
                        payload={"reason": partial_reason},
                    ))
        self.sessions.transition(
            self.capture_session_id, target=CaptureSessionState.COVERAGE_FINALIZING,
            source_ts=source_ts,
            reason="EVIDENCE_DURABLE" if durable else "EVIDENCE_EXPLICIT_PARTIAL",
            payload={"partial_reason": partial_reason} if explicit_partial else None,
        )

    def finalize_attempt(
        self, *, capture_attempt_id: str, call_ref: str | None,
        channel_evidence: dict, channel_applicability: dict | None,
        signals: list, required_channels_for_diagnosis: tuple[str, ...],
        independent_support_count: int, contradictions: tuple[str, ...] = (),
    ) -> FinalizedAttemptResult:
        if self.sessions.state(self.capture_session_id) != CaptureSessionState.COVERAGE_FINALIZING:
            raise CaptureV2Error("COVERAGE_FINALIZATION_NOT_ACTIVE")
        with self.session_factory() as db:
            attempt = db.get(CaptureAttempt, capture_attempt_id)
            if attempt is None:
                raise CaptureV2Error("CAPTURE_ATTEMPT_NOT_FOUND")
            state = attempt.state
        if state == CaptureAttemptState.ENDED.value:
            self.attempt_flow.begin_evidence_finalizing(capture_attempt_id)
        elif state not in (
            CaptureAttemptState.EVIDENCE_FINALIZING.value,
            CaptureAttemptState.EVALUATED.value,
        ):
            raise CaptureV2Error(
                "ATTEMPT_NOT_FINALIZABLE", details={"state": state}
            )

        window_id, coverage_status = self.stack.e.finalize_attempt(
            capture_session_id=self.capture_session_id,
            capture_attempt_id=capture_attempt_id, call_ref=call_ref,
            channel_evidence=channel_evidence,
            channel_applicability=channel_applicability,
        )
        quality_id, quality = self.stack.f.evaluate_from_coverage(
            coverage_window_id=window_id,
            capture_session_id=self.capture_session_id,
            capture_attempt_id=capture_attempt_id, call_ref=call_ref,
            signals=signals,
            required_channels_for_diagnosis=required_channels_for_diagnosis,
            independent_support_count=independent_support_count,
            contradictions=contradictions,
        )
        self.attempt_flow.mark_evaluated(
            capture_attempt_id, quality_snapshot_id=quality_id
        )
        try:
            self.binding.update_call_state(
                capture_attempt_id=capture_attempt_id, state="QUALITY_FINALIZED",
                source_ts=utcnow(), details={"quality_snapshot_id": quality_id},
            )
        except CaptureV2Error as exc:
            if exc.code != "CAPTURE_CALL_NOT_BOUND":
                raise
        return FinalizedAttemptResult(
            capture_attempt_id=capture_attempt_id,
            coverage_window_id=window_id,
            coverage_status=coverage_status.value,
            quality_snapshot_id=quality_id,
            quality=quality,
        )

    def build_report_and_mark_analyzed(
        self, *, quality_snapshot_id: str, findings: list,
        source_ts: datetime | None = None,
    ) -> dict:
        report = self.stack.f.build_report_from_snapshot(
            capture_session_id=self.capture_session_id,
            quality_snapshot_id=quality_snapshot_id, findings=findings,
        )
        with self.session_factory() as db:
            quality = db.get(QualitySnapshot, quality_snapshot_id)
            if quality is None or quality.capture_session_id != self.capture_session_id:
                raise CaptureV2Error("QUALITY_SNAPSHOT_NOT_FOUND")
            attempt_id = quality.capture_attempt_id
        if attempt_id:
            try:
                self.binding.update_call_state(
                    capture_attempt_id=attempt_id, state="ANALYZED",
                    source_ts=source_ts or utcnow(),
                    details={"quality_snapshot_id": quality_snapshot_id},
                )
            except CaptureV2Error as exc:
                if exc.code != "CAPTURE_CALL_NOT_BOUND":
                    raise
        return report

    def begin_cleanup(self, *, source_ts: datetime | None = None) -> None:
        if self.sessions.state(self.capture_session_id) != CaptureSessionState.COVERAGE_FINALIZING:
            raise CaptureV2Error("CLEANUP_NOT_ALLOWED")
        with self.session_factory() as db:
            unfinished = list(db.scalars(select(CaptureAttempt).where(
                CaptureAttempt.capture_session_id == self.capture_session_id,
                CaptureAttempt.state.not_in((
                    CaptureAttemptState.EVALUATED.value,
                    CaptureAttemptState.CLASSIFIED_GLITCH.value,
                )),
            )))
        if unfinished:
            raise CaptureV2Error(
                "CLEANUP_BLOCKED_ATTEMPTS_NOT_FINALIZED",
                details={"attempt_ids": [a.id for a in unfinished]},
            )
        self.sessions.transition(
            self.capture_session_id, target=CaptureSessionState.CLEANUP,
            source_ts=source_ts, reason="ALL_ATTEMPTS_FINALIZED",
        )

    def complete_from_cleanup(self, *, source_ts: datetime | None = None) -> None:
        with self.session_factory() as db:
            row = db.get(CaptureSession, self.capture_session_id)
            if row is None:
                raise CaptureV2Error("CAPTURE_SESSION_NOT_FOUND")
            if row.cleanup_status != "VERIFIED":
                raise CaptureV2Error("CLEANUP_NOT_VERIFIED", details={"cleanup_status": row.cleanup_status})
        self.sessions.transition(
            self.capture_session_id, target=CaptureSessionState.COMPLETED,
            source_ts=source_ts, reason="CLEANUP_VERIFIED",
        )

    def complete(self, *, cleanup_verified: bool,
                 source_ts: datetime | None = None) -> None:
        # Compatibility/test API. Production orchestration must use
        # complete_from_cleanup(), which reads the persisted Cleanup Step Ledger.
        if not cleanup_verified:
            raise CaptureV2Error("CLEANUP_NOT_VERIFIED")
        self.complete_from_cleanup(source_ts=source_ts)

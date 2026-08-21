from __future__ import annotations

from datetime import datetime

from app.capture_v2.fxs.attempt_service import AttemptSemanticRepository
from app.capture_v2.fxs.sanitizer import FxsEventSanitizer, RawFxsEvent
from app.capture_v2.readiness.data_plane import AttemptDataPlaneVerifier, ChannelExpectation
from app.capture_v2.readiness.stage1 import (
    CapturePathChecks, CapturePathReadinessEvaluator, ReadinessRepository,
)
from app.capture_v2.readiness.watchdog import CaptureWatchdog, WatchdogInputs


class CaptureV2DSession:
    """Software composition for Phase-D semantics/readiness.

    Real FXS stream wiring is deliberately outside this class so APF real-gates can
    be deferred without preventing deterministic logic implementation/testing.
    """

    def __init__(self, *, capture_session_id: str, session_factory, effective_profile: dict):
        self.capture_session_id = capture_session_id
        self.session_factory = session_factory
        resolved = dict(effective_profile.get("resolved") or effective_profile)
        fxs_cfg = dict(resolved.get("fxs") or {})
        self.readiness_cfg = dict(resolved.get("readiness") or {})
        self.sanitizer = FxsEventSanitizer(
            hook_glitch_max_ms=int(fxs_cfg.get("hook_glitch_max_ms") or 100),
            post_onhook_rebound_window_ms=int(fxs_cfg.get("post_onhook_rebound_window_ms") or 500),
            stable_offhook_confirm_ms=int(fxs_cfg.get("stable_offhook_confirm_ms") or 100),
            hook_flash_min_ms=int(fxs_cfg.get("hook_flash_min_ms") or 100),
            hook_flash_max_ms=int(fxs_cfg.get("hook_flash_max_ms") or 1000),
        )
        self.attempts = AttemptSemanticRepository(session_factory)
        self.data_plane = AttemptDataPlaneVerifier(session_factory)
        self.readiness = ReadinessRepository(session_factory)

    def evaluate_stage1(self, checks: CapturePathChecks):
        decision = CapturePathReadinessEvaluator.evaluate(checks)
        self.readiness.persist_stage1(capture_session_id=self.capture_session_id, decision=decision)
        return decision

    def ingest_fxs(self, event: RawFxsEvent, *, call_active: bool = False) -> tuple[str, ...]:
        self.attempts.append_raw_event(
            capture_session_id=self.capture_session_id, source_ts=event.source_ts,
            event=event.event, digit=event.digit, line=event.line,
        )
        ids = []
        for action in self.sanitizer.on_raw(event, call_active=call_active):
            attempt_id = self.attempts.apply(capture_session_id=self.capture_session_id, action=action)
            if attempt_id:
                ids.append(attempt_id)
        return tuple(dict.fromkeys(ids))

    def confirm_business_evidence(self, *, source_ts: datetime, source: str) -> tuple[str, ...]:
        ids = []
        for action in self.sanitizer.confirm_business_evidence(source_ts=source_ts, source=source):
            attempt_id = self.attempts.apply(capture_session_id=self.capture_session_id, action=action)
            if attempt_id:
                ids.append(attempt_id)
        return tuple(ids)

    def tick_hook_stability(self, *, now: datetime) -> tuple[str, ...]:
        ids = []
        for action in self.sanitizer.confirm_if_stable(now):
            attempt_id = self.attempts.apply(capture_session_id=self.capture_session_id, action=action)
            if attempt_id:
                ids.append(attempt_id)
        return tuple(ids)


    def flush_pending_onhook(self, *, now: datetime) -> tuple[str, ...]:
        ids = []
        for action in self.sanitizer.flush_pending_onhook(now):
            attempt_id = self.attempts.apply(capture_session_id=self.capture_session_id, action=action)
            if attempt_id:
                ids.append(attempt_id)
        return tuple(ids)

    def create_channel_expectation(self, *, capture_attempt_id: str, channel: str,
                                   trigger_ts: datetime, applicable: bool = True,
                                   details: dict | None = None) -> str:
        key = channel.lower()
        timeout_map = {
            "pcm_rx": self.readiness_cfg.get("pcm_readiness_timeout_seconds", 1.0),
            "pcm_tx": self.readiness_cfg.get("pcm_readiness_timeout_seconds", 1.0),
            "sip": self.readiness_cfg.get("sip_expectation_timeout_seconds", 3.0),
            "rtp": self.readiness_cfg.get("rtp_expectation_timeout_seconds", 3.0),
            "pcm_media": self.readiness_cfg.get("pcm_media_expectation_timeout_seconds", 2.0),
            "fxs": None,
            "pcap": None,
        }
        if key not in timeout_map:
            raise ValueError(f"UNKNOWN_CHANNEL_EXPECTATION:{channel}")
        return self.data_plane.create_expectation(
            capture_attempt_id=capture_attempt_id,
            expectation=ChannelExpectation(channel=channel.upper(), timeout_seconds=timeout_map[key], applicable=applicable),
            expectation_created_at=trigger_ts, details=details,
        )

    def observe_channel(self, *, capture_attempt_id: str, channel: str,
                        source_ts: datetime, details: dict | None = None) -> None:
        self.data_plane.observe(
            capture_attempt_id=capture_attempt_id, channel=channel.upper(),
            source_ts=source_ts, details=details,
        )

    def expire_expectations(self, *, capture_attempt_id: str, now: datetime) -> tuple[str, ...]:
        return self.data_plane.expire_due(capture_attempt_id=capture_attempt_id, now=now)

    def evaluate_watchdog(self, inputs: WatchdogInputs):
        decision = CaptureWatchdog.evaluate(inputs)
        if not decision.healthy:
            self.readiness.revoke_stage1(
                capture_session_id=self.capture_session_id,
                reasons=list(decision.reasons),
            )
        return decision

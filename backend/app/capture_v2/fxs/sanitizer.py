from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.capture_v2.errors import CaptureV2Error


class SemanticActionType(StrEnum):
    PROVISIONAL_ATTEMPT = "PROVISIONAL_ATTEMPT"
    CONFIRMED_ATTEMPT = "CONFIRMED_ATTEMPT"
    ATTEMPT_ENDED = "ATTEMPT_ENDED"
    FXS_HOOK_GLITCH = "FXS_HOOK_GLITCH"
    FXS_HOOK_FLASH = "FXS_HOOK_FLASH"
    DTMF = "DTMF"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class RawFxsEvent:
    source_ts: datetime
    event: str
    digit: str | None = None
    line: int = 0


@dataclass(frozen=True)
class SemanticFxsAction:
    action: SemanticActionType
    source_ts: datetime
    details: dict


@dataclass
class _Provisional:
    start_ts: datetime
    rebound_candidate: bool
    evidence_seen: bool = False


@dataclass
class _PendingCallOnhook:
    source_ts: datetime


class FxsEventSanitizer:
    """Semantic debounce without deleting raw evidence.

    Raw events must be written to CaptureEvent before this class is called. This
    state machine only decides business semantics. Capture is already continuous,
    so delaying semantic confirmation never delays packet/audio evidence.

    Two independent short-pulse cases are kept distinct:
    * post-hangup ONHOOK->OFFHOOK->ONHOOK rebound: FXS_HOOK_GLITCH, no Attempt;
    * during-call OFFHOOK->ONHOOK->OFFHOOK pulse: FXS_HOOK_FLASH, same Attempt.
    """

    def __init__(self, *, hook_glitch_max_ms: int = 100,
                 post_onhook_rebound_window_ms: int = 500,
                 stable_offhook_confirm_ms: int = 100,
                 hook_flash_min_ms: int = 100,
                 hook_flash_max_ms: int = 1000):
        if hook_flash_min_ms < 0 or hook_flash_max_ms <= hook_flash_min_ms:
            raise ValueError("FXS_HOOK_FLASH_WINDOW_INVALID")
        self.glitch = timedelta(milliseconds=hook_glitch_max_ms)
        self.rebound = timedelta(milliseconds=post_onhook_rebound_window_ms)
        self.stable = timedelta(milliseconds=stable_offhook_confirm_ms)
        self.flash_min = timedelta(milliseconds=hook_flash_min_ms)
        self.flash_max = timedelta(milliseconds=hook_flash_max_ms)
        self.semantic_hook = "ONHOOK"
        self.last_confirmed_onhook: datetime | None = None
        self.provisional: _Provisional | None = None
        self.pending_call_onhook: _PendingCallOnhook | None = None

    def _confirm(self, ts: datetime, source: str) -> SemanticFxsAction | None:
        if self.provisional is None:
            return None
        start = self.provisional.start_ts
        self.provisional = None
        self.semantic_hook = "OFFHOOK"
        return SemanticFxsAction(
            SemanticActionType.CONFIRMED_ATTEMPT, start,
            {"confirmation_source": source, "confirmed_at": ts.isoformat()},
        )

    def confirm_business_evidence(self, *, source_ts: datetime, source: str) -> tuple[SemanticFxsAction, ...]:
        action = self._confirm(source_ts, source)
        return (action,) if action else ()

    def confirm_if_stable(self, now: datetime) -> tuple[SemanticFxsAction, ...]:
        if self.provisional is None:
            return ()
        if now - self.provisional.start_ts < self.stable:
            return ()
        action = self._confirm(now, "HOOK_STABLE")
        return (action,) if action else ()

    def flush_pending_onhook(self, now: datetime) -> tuple[SemanticFxsAction, ...]:
        """Confirm a during-call ONHOOK as a real end after flash window expires."""
        pending = self.pending_call_onhook
        if pending is None:
            return ()
        duration = now - pending.source_ts
        if duration < timedelta(0):
            raise CaptureV2Error("SOURCE_TIME_REGRESSION")
        if duration <= self.flash_max:
            return ()
        self.pending_call_onhook = None
        self.semantic_hook = "ONHOOK"
        self.last_confirmed_onhook = pending.source_ts
        return (SemanticFxsAction(
            SemanticActionType.ATTEMPT_ENDED, pending.source_ts,
            {"confirmation_source": "HOOK_FLASH_WINDOW_EXPIRED",
             "confirmed_at": now.isoformat()},
        ),)

    def on_raw(self, event: RawFxsEvent, *, call_active: bool = False) -> tuple[SemanticFxsAction, ...]:
        kind = event.event.upper().strip()

        if kind == "OFFHOOK" and self.pending_call_onhook is not None:
            pending = self.pending_call_onhook
            duration = event.source_ts - pending.source_ts
            if duration < timedelta(0):
                raise CaptureV2Error("SOURCE_TIME_REGRESSION")
            self.pending_call_onhook = None
            if duration < self.flash_min:
                # Very short ONHOOK pulse during a call is electrical/mechanical
                # bounce, not a real end and not a new Attempt.
                self.semantic_hook = "OFFHOOK"
                return (SemanticFxsAction(
                    SemanticActionType.FXS_HOOK_GLITCH, event.source_ts,
                    {"direction": "ONHOOK_PULSE", "duration_ms": int(duration.total_seconds() * 1000),
                     "candidate_end": pending.source_ts.isoformat()},
                ),)
            if duration <= self.flash_max:
                self.semantic_hook = "OFFHOOK"
                return (SemanticFxsAction(
                    SemanticActionType.FXS_HOOK_FLASH, event.source_ts,
                    {"duration_ms": int(duration.total_seconds() * 1000),
                     "flash_onhook_ts": pending.source_ts.isoformat()},
                ),)
            # The ONHOOK lasted too long to be a flash. End the old Attempt at the
            # original source timestamp, then treat this OFFHOOK as a fresh candidate.
            self.semantic_hook = "ONHOOK"
            self.last_confirmed_onhook = pending.source_ts
            rebound_candidate = (
                timedelta(0) <= event.source_ts - self.last_confirmed_onhook <= self.rebound
            )
            self.provisional = _Provisional(event.source_ts, rebound_candidate)
            return (
                SemanticFxsAction(
                    SemanticActionType.ATTEMPT_ENDED, pending.source_ts,
                    {"confirmation_source": "HOOK_FLASH_WINDOW_EXCEEDED"},
                ),
                SemanticFxsAction(
                    SemanticActionType.PROVISIONAL_ATTEMPT, event.source_ts,
                    {"post_onhook_rebound_candidate": rebound_candidate},
                ),
            )

        if kind == "OFFHOOK":
            if self.provisional is not None or self.semantic_hook == "OFFHOOK":
                return (SemanticFxsAction(SemanticActionType.DUPLICATE, event.source_ts,
                                          {"event": "OFFHOOK"}),)
            rebound_candidate = (
                self.last_confirmed_onhook is not None
                and timedelta(0) <= event.source_ts - self.last_confirmed_onhook <= self.rebound
            )
            self.provisional = _Provisional(event.source_ts, rebound_candidate)
            return (SemanticFxsAction(
                SemanticActionType.PROVISIONAL_ATTEMPT, event.source_ts,
                {"post_onhook_rebound_candidate": rebound_candidate},
            ),)

        if kind == "DTMF":
            actions = list(self.confirm_business_evidence(source_ts=event.source_ts, source="DTMF"))
            actions.append(SemanticFxsAction(
                SemanticActionType.DTMF, event.source_ts, {"digit": event.digit},
            ))
            return tuple(actions)

        if kind == "ONHOOK":
            if self.pending_call_onhook is not None:
                return (SemanticFxsAction(SemanticActionType.DUPLICATE, event.source_ts,
                                          {"event": "ONHOOK", "pending_flash_resolution": True}),)
            if self.provisional is not None:
                p = self.provisional
                duration = event.source_ts - p.start_ts
                if duration < timedelta(0):
                    raise CaptureV2Error("SOURCE_TIME_REGRESSION")
                self.provisional = None
                if p.rebound_candidate and duration <= self.glitch and not p.evidence_seen:
                    self.semantic_hook = "ONHOOK"
                    return (SemanticFxsAction(
                        SemanticActionType.FXS_HOOK_GLITCH, event.source_ts,
                        {"duration_ms": int(duration.total_seconds() * 1000),
                         "candidate_start": p.start_ts.isoformat()},
                    ),)
                self.semantic_hook = "OFFHOOK"
                confirmed = SemanticFxsAction(
                    SemanticActionType.CONFIRMED_ATTEMPT, p.start_ts,
                    {"confirmation_source": "ONHOOK_NON_GLITCH",
                     "confirmed_at": event.source_ts.isoformat()},
                )
                self.semantic_hook = "ONHOOK"
                self.last_confirmed_onhook = event.source_ts
                ended = SemanticFxsAction(
                    SemanticActionType.ATTEMPT_ENDED, event.source_ts,
                    {"duration_ms": int(duration.total_seconds() * 1000)},
                )
                return confirmed, ended
            if self.semantic_hook == "OFFHOOK":
                if call_active:
                    self.pending_call_onhook = _PendingCallOnhook(event.source_ts)
                    return ()
                self.semantic_hook = "ONHOOK"
                self.last_confirmed_onhook = event.source_ts
                return (SemanticFxsAction(
                    SemanticActionType.ATTEMPT_ENDED, event.source_ts, {},
                ),)
            self.last_confirmed_onhook = event.source_ts
            return (SemanticFxsAction(SemanticActionType.DUPLICATE, event.source_ts,
                                      {"event": "ONHOOK"}),)

        raise CaptureV2Error("FXS_EVENT_UNSUPPORTED", details={"event": event.event})

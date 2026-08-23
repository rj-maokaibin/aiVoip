from datetime import datetime, timedelta, timezone

from app.capture_v2.enums import ReadinessStatus
from app.capture_v2.fxs.sanitizer import FxsEventSanitizer, RawFxsEvent, SemanticActionType
from app.capture_v2.readiness.stage1 import CapturePathChecks, CapturePathReadinessEvaluator


def t(ms):
    return datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc) + timedelta(milliseconds=ms)


def test_apf3260_post_onhook_20ms_offhook_20ms_onhook_is_glitch_not_attempt():
    s = FxsEventSanitizer(hook_glitch_max_ms=100, post_onhook_rebound_window_ms=500,
                          stable_offhook_confirm_ms=100)
    # prior confirmed/semantic onhook edge
    s.on_raw(RawFxsEvent(t(0), "ONHOOK"))
    p = s.on_raw(RawFxsEvent(t(20), "OFFHOOK"))
    assert p[0].action == SemanticActionType.PROVISIONAL_ATTEMPT
    assert p[0].details["post_onhook_rebound_candidate"] is True
    end = s.on_raw(RawFxsEvent(t(40), "ONHOOK"))
    assert [x.action for x in end] == [SemanticActionType.FXS_HOOK_GLITCH]
    assert end[0].details["duration_ms"] == 20


def test_fast_user_attempt_outside_rebound_is_not_swallowed():
    s = FxsEventSanitizer(hook_glitch_max_ms=100, post_onhook_rebound_window_ms=100,
                          stable_offhook_confirm_ms=100)
    s.on_raw(RawFxsEvent(t(0), "ONHOOK"))
    s.on_raw(RawFxsEvent(t(500), "OFFHOOK"))
    out = s.on_raw(RawFxsEvent(t(550), "ONHOOK"))
    assert [x.action for x in out] == [
        SemanticActionType.CONFIRMED_ATTEMPT,
        SemanticActionType.ATTEMPT_ENDED,
    ]


def test_dtmf_confirms_provisional_without_waiting_stable_timer():
    s = FxsEventSanitizer(stable_offhook_confirm_ms=100)
    s.on_raw(RawFxsEvent(t(0), "OFFHOOK"))
    out = s.on_raw(RawFxsEvent(t(20), "DTMF", digit="1"))
    assert [x.action for x in out] == [SemanticActionType.CONFIRMED_ATTEMPT, SemanticActionType.DTMF]


def test_stage1_requires_every_capture_path_check():
    ok = CapturePathChecks(True, True, True, True, True, True, True, True, True, True)
    assert CapturePathReadinessEvaluator.evaluate(ok).status == ReadinessStatus.READY
    bad = CapturePathChecks(True, True, True, True, False, True, True, True, True, True)
    decision = CapturePathReadinessEvaluator.evaluate(bad)
    assert decision.status == ReadinessStatus.PENDING
    assert "FXS_READY_NOT_READY" in decision.reasons


def test_during_call_hook_flash_does_not_end_or_create_new_attempt():
    s = FxsEventSanitizer(hook_flash_min_ms=100, hook_flash_max_ms=1000,
                          stable_offhook_confirm_ms=100)
    s.on_raw(RawFxsEvent(t(0), "OFFHOOK"))
    confirmed = s.confirm_if_stable(t(100))
    assert confirmed[0].action == SemanticActionType.CONFIRMED_ATTEMPT
    # During an active call, ONHOOK is held semantically while capture continues.
    assert s.on_raw(RawFxsEvent(t(1000), "ONHOOK"), call_active=True) == ()
    out = s.on_raw(RawFxsEvent(t(1300), "OFFHOOK"), call_active=True)
    assert [x.action for x in out] == [SemanticActionType.FXS_HOOK_FLASH]
    assert out[0].details["duration_ms"] == 300
    assert s.semantic_hook == "OFFHOOK"
    assert s.provisional is None


def test_during_call_short_onhook_pulse_is_glitch_not_end():
    s = FxsEventSanitizer(hook_flash_min_ms=100, hook_flash_max_ms=1000,
                          stable_offhook_confirm_ms=100)
    s.on_raw(RawFxsEvent(t(0), "OFFHOOK")); s.confirm_if_stable(t(100))
    assert s.on_raw(RawFxsEvent(t(1000), "ONHOOK"), call_active=True) == ()
    out = s.on_raw(RawFxsEvent(t(1020), "OFFHOOK"), call_active=True)
    assert [x.action for x in out] == [SemanticActionType.FXS_HOOK_GLITCH]
    assert out[0].details["direction"] == "ONHOOK_PULSE"
    assert s.semantic_hook == "OFFHOOK"


def test_during_call_real_hangup_uses_original_onhook_source_time_after_flash_window():
    s = FxsEventSanitizer(hook_flash_min_ms=100, hook_flash_max_ms=1000,
                          stable_offhook_confirm_ms=100)
    s.on_raw(RawFxsEvent(t(0), "OFFHOOK")); s.confirm_if_stable(t(100))
    assert s.on_raw(RawFxsEvent(t(1000), "ONHOOK"), call_active=True) == ()
    assert s.flush_pending_onhook(t(1500)) == ()
    out = s.flush_pending_onhook(t(2001))
    assert [x.action for x in out] == [SemanticActionType.ATTEMPT_ENDED]
    assert out[0].source_ts == t(1000)
    assert s.semantic_hook == "ONHOOK"

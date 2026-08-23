"""Replay small immutable slices from the 2026-08-20 real-DUT Golden AIM logs.

These are regression fixtures for parsing/source-time semantics only. They do
NOT promote R4/R5 to a release-grade real Gate: Hook Flash calibration and the
finalized PCAP/PCM/Coverage bundle remain separate evidence requirements.

Sources (operator-collected real DUT transcripts):
- APF1250-B.log, Golden call 601 -> 301, 2026-08-20
- APF3260-B.log, Golden call 101 -> 301, 2026-08-20
"""

from datetime import datetime, timedelta

from app.capture_v2.fxs.sanitizer import FxsEventSanitizer, RawFxsEvent, SemanticActionType
from app.reproduction.fxs_event_monitor import FxsEventMonitor


APF1250_GOLDEN_SLICE = """
2026-08-20 18:21:28.978000 [0] D:: [D]OFFHOOK
2026-08-20 18:22:17.566000 [0] D:: [D]DTMF<1>
2026-08-20 18:22:18.426000 [0] D:: [D]DTMF<2>
2026-08-20 18:22:19.186000 [0] D:: [D]DTMF<3>
2026-08-20 18:22:20.006000 [0] D:: [D]DTMF<#>
2026-08-20 18:22:27.774000 [0] D:: [D]ONHOOK
"""

APF3260_GOLDEN_SLICE = """
2026-08-20 19:47:54.269000 [0] D:: [D]OFFHOOK
2026-08-20 19:48:38.583000 [0] D:: [D]DTMF<1>
2026-08-20 19:48:38.963000 [0] D:: [D]DTMF<2>
2026-08-20 19:48:39.383000 [0] D:: [D]DTMF<3>
2026-08-20 19:48:40.323000 [0] D:: [D]DTMF<#>
2026-08-20 19:48:59.703000 [0] D:: [D]ONHOOK
"""


def _parse(raw: str):
    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None)
    return monitor.parse_full_output(raw)


def _source_ts(text: str) -> datetime:
    # The Aug-20 DUTs used the local +0800 clock. Keep Source Time explicit in
    # replay so ingestion order can never silently fall back to processing time.
    return datetime.strptime(text + " +0800", "%Y-%m-%d %H:%M:%S.%f %z")


def test_apf1250_real_golden_slice_parses_exact_fxs_order_and_source_time():
    events = _parse(APF1250_GOLDEN_SLICE)
    assert [(e.event, e.digit) for e in events] == [
        ("OFFHOOK", None), ("DTMF", "1"), ("DTMF", "2"),
        ("DTMF", "3"), ("DTMF", "#"), ("ONHOOK", None),
    ]
    assert events[0].timestamp == "2026-08-20 18:21:28.978000"
    assert events[-1].timestamp == "2026-08-20 18:22:27.774000"


def test_apf3260_real_golden_slice_parses_exact_fxs_order_and_source_time():
    events = _parse(APF3260_GOLDEN_SLICE)
    assert [(e.event, e.digit) for e in events] == [
        ("OFFHOOK", None), ("DTMF", "1"), ("DTMF", "2"),
        ("DTMF", "3"), ("DTMF", "#"), ("ONHOOK", None),
    ]
    assert events[0].timestamp == "2026-08-20 19:47:54.269000"
    assert events[-1].timestamp == "2026-08-20 19:48:59.703000"


def test_real_golden_slices_bind_dtmf_and_end_at_original_onhook_source_time():
    for raw in (APF1250_GOLDEN_SLICE, APF3260_GOLDEN_SLICE):
        parsed = _parse(raw)
        sanitizer = FxsEventSanitizer(stable_offhook_confirm_ms=100)
        actions = []
        for event in parsed:
            source_ts = _source_ts(event.timestamp)
            if event.event == "ONHOOK":
                # Golden transcript proves the call is established before the
                # in-call 123# sequence. Model the real hangup as call-active;
                # semantic end must retain the physical ONHOOK Source Time.
                actions.extend(sanitizer.on_raw(
                    RawFxsEvent(source_ts, event.event, digit=event.digit, line=event.line),
                    call_active=True,
                ))
                actions.extend(sanitizer.flush_pending_onhook(
                    source_ts + timedelta(milliseconds=1001)
                ))
            else:
                actions.extend(sanitizer.on_raw(
                    RawFxsEvent(source_ts, event.event, digit=event.digit, line=event.line),
                    call_active=event.event == "DTMF",
                ))

        kinds = [a.action for a in actions]
        assert kinds.count(SemanticActionType.CONFIRMED_ATTEMPT) == 1
        assert kinds.count(SemanticActionType.DTMF) == 4
        assert kinds.count(SemanticActionType.ATTEMPT_ENDED) == 1
        end = [a for a in actions if a.action == SemanticActionType.ATTEMPT_ENDED][0]
        assert end.source_ts == _source_ts(parsed[-1].timestamp)

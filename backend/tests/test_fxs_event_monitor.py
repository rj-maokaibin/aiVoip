from __future__ import annotations

from app.reproduction.fxs_event_monitor import FxsEventMonitor, FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE


def _chunked(*chunks):
    it = iter(chunks)
    return lambda: next(it, None)


def _collector(events):
    def hook(ev):
        events.append((ev.timestamp, ev.line, ev.event, ev.digit))
    return hook


def test_full_debug_enable_and_disable_sequences_are_sent():
    commands: list[str] = []
    m = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=commands.append)
    m.enable_debug()
    assert commands == FULL_DEBUG_ENABLE
    m.disable_debug()
    assert commands == FULL_DEBUG_ENABLE + FULL_DEBUG_DISABLE


def test_monitor_parses_offhook_dtmf_onhook_from_interleaved_stream():
    events = []
    m = FxsEventMonitor(
        read_aim_chunk=_chunked(
            # IPC noise first, then FXS events interleaved.
            '2026-08-13 22:52:53.000000 [--]IPC:: [D]--> Message Received\n'
            '2026-08-13 22:52:53.878000 [0] D:: [D]OFFHOOK\n'
            '2026-08-13 22:52:54.778000 [0] D:: [D]DTMF<1>\n'
            '2026-08-13 22:52:58.758000 [0] D:: [D]ONHOOK\n',
        ),
        write_aim=lambda _: None,
        event_hook=_collector(events),
    )
    m.start()
    parsed = m.poll()

    assert [e.event for e in parsed] == ['OFFHOOK', 'DTMF', 'ONHOOK']
    assert [e.digit for e in parsed] == [None, '1', None]
    assert parsed[0].line == 0
    assert events == [
        ('2026-08-13 22:52:53.878000', 0, 'OFFHOOK', None),
        ('2026-08-13 22:52:54.778000', 0, 'DTMF', '1'),
        ('2026-08-13 22:52:58.758000', 0, 'ONHOOK', None),
    ]


def test_monitor_strips_ansi_color_codes_from_real_dut_stream():
    # The APF1250 colors event lines; escape codes prefix each event line.
    raw = (
        '\x1b[33m2026-08-13 23:10:41.382000 [0] D:: [D]OFFHOOK\n'
        '\x1b[m\x1b[36m2026-08-13 23:10:42.282000 [0] D:: [D]DTMF<1>\n'
        '\x1b[m\x1b[33m2026-08-13 23:10:46.982000 [0] D:: [D]ONHOOK\n'
    )
    m = FxsEventMonitor(read_aim_chunk=_chunked(raw), write_aim=lambda _: None)
    m.start()
    parsed = m.poll()
    assert [e.event for e in parsed] == ['OFFHOOK', 'DTMF', 'ONHOOK']
    assert parsed[1].digit == '1'


def test_monitor_accumulates_partial_line_across_chunks():
    m = FxsEventMonitor(
        read_aim_chunk=_chunked(
            '2026-08-13 22:52:53.878000 [0] D:: [D]OFF',  # partial line
            'HOOK\n',
        ),
        write_aim=lambda _: None,
    )
    m.start()
    first = m.poll()
    assert first == []
    second = m.poll()
    assert [e.event for e in second] == ['OFFHOOK']


def test_monitor_ignores_stream_when_not_started():
    m = FxsEventMonitor(read_aim_chunk=_chunked('OFFHOOK\n'), write_aim=lambda _: None)
    assert m.poll() == []


def test_monitor_stop_sends_disable_sequence():
    commands: list[str] = []
    m = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=commands.append)
    m.start()
    commands.clear()
    m.stop()
    assert commands == FULL_DEBUG_DISABLE
    assert m.poll() == []

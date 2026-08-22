from __future__ import annotations

import struct

from app.reports.evidence_visuals import (
    _rtp_event_label,
    _sip_message_label,
    _sip_messages,
    render_rtp_timeline_png,
    render_sip_call_flow_png,
    render_spectrogram_png,
    render_waveform_png,
)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_waveform_is_deterministic_and_meets_minimum_width_with_rms():
    waveform = {
        "duration_seconds": 1.0,
        "bins": [
            {"t": 0.0, "min": -1000, "max": 1000, "rms_dbfs": -32.0},
            {"t": 0.5, "min": -2000, "max": 2000, "rms_dbfs": -24.0},
            {"t": 1.0, "min": -500, "max": 500, "rms_dbfs": -40.0},
        ],
    }
    a = render_waveform_png(waveform, anomaly_start=0.4, anomaly_end=0.7)
    b = render_waveform_png(waveform, anomaly_start=0.4, anomaly_end=0.7)
    assert a == b
    width, height = _png_size(a)
    assert width >= 1200
    assert height > 0


def test_spectrogram_is_deterministic_and_meets_minimum_width_with_relative_db_scale():
    spec = {
        "times": [0.0, 0.1],
        "frequencies": [0.0, 1000.0],
        "db": [[-90.0, -40.0], [-70.0, -20.0]],
    }
    a = render_spectrogram_png(spec, anomaly_start=0.02, anomaly_end=0.08)
    b = render_spectrogram_png(spec, anomaly_start=0.02, anomaly_end=0.08)
    assert a == b
    assert _png_size(a)[0] >= 1200


def test_rtp_event_label_exposes_time_frame_sequence_loss_jitter_delta():
    label = _rtp_event_label({
        "type": "HIGH_DELTA",
        "start_time": 12.5,
        "details": {
            "previous_frame_number": 100,
            "current_frame_number": 101,
            "previous_sequence": 2000,
            "current_sequence": 2001,
            "lost_packets": 0,
            "p95_jitter_ms": 7.5,
            "delta_ms": 88.0,
        },
    }, 10.0)
    assert "T+2.500S" in label
    assert "F100>101" in label
    assert "SEQ 2000>2001" in label
    assert "LOST 0" in label
    assert "JIT 7.5MS" in label
    assert "DELTA 88MS" in label

    png = render_rtp_timeline_png([{
        "src_ip": "192.0.2.1", "src_port": 10000,
        "dst_ip": "192.0.2.2", "dst_port": 20000,
        "start_time": 10.0, "end_time": 13.0,
        "events": [{"type": "HIGH_DELTA", "start_time": 12.5, "details": {"delta_ms": 88.0}}],
    }])
    assert _png_size(png)[0] >= 1200


def test_sip_renderer_consumes_production_ladder_and_exposes_frame_cseq_status():
    call = {
        "call_id": "call-123",
        "caller": "sip:8000@example.test",
        "callee": "sip:601@example.test",
        "ladder": [
            {"frame_number": 10, "src": "192.0.2.10:5060", "dst": "192.0.2.20:5060", "method": "INVITE", "cseq": 1, "cseq_method": "INVITE"},
            {"frame_number": 11, "src": "192.0.2.20:5060", "dst": "192.0.2.10:5060", "status_code": 200, "reason": "OK", "cseq": 1, "cseq_method": "INVITE"},
        ],
    }
    assert _sip_messages([call]) == call["ladder"]
    assert _sip_message_label(call["ladder"][0]).startswith("F10 INVITE")
    assert "CSEQ 1 INVITE" in _sip_message_label(call["ladder"][1])
    png = render_sip_call_flow_png([call])
    assert _png_size(png)[0] >= 1200

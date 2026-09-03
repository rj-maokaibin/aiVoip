import io
import wave

from app.reports.v2.artifact_binding import (
    render_event_audio_clip,
    source_unavailable_audio_binding,
)


def _wav(*, seconds=3.0, sample_rate=8000, sample_width=2):
    frames = int(seconds * sample_rate)
    data = (b"\x00\x00" if sample_width == 2 else b"\x00") * frames
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(data)
    return out.getvalue()


def test_event_audio_clip_binds_event_finding_source_time_and_hash():
    clip, record = render_event_audio_clip(
        _wav(),
        event={"event_id": "EV-1", "timestamp": 101.5},
        source_artifact={"artifact_id": "PCM-RX-1", "time_range": {"start": 100.0, "end": 103.0}},
        finding_id="F-1",
    )

    assert clip
    assert record["status"] == "AVAILABLE"
    assert record["event_refs"] == ["EV-1"]
    assert record["finding_refs"] == ["F-1"]
    assert record["source_artifact_ids"] == ["PCM-RX-1"]
    assert record["time_range"] == {"start": 100.5, "end": 102.5}
    assert len(record["sha256"]) == 64
    assert record["size"] == len(clip)
    assert record["mime_type"] == "audio/wav"


def test_unsupported_wav_returns_structured_failure_with_source_available():
    clip, record = render_event_audio_clip(
        _wav(sample_width=1),
        event={"event_id": "EV-1", "timestamp": 101.5},
        source_artifact={"artifact_id": "PCM-RX-1", "start_time": 100.0},
        finding_id="F-1",
    )

    assert clip == b""
    assert record == {
        "artifact_requirement": "AUDIO_CLIP",
        "status": "FAILED",
        "reason_code": "UNSUPPORTED_CODEC",
        "source_available": True,
        "finding_refs": ["F-1"],
        "event_refs": ["EV-1"],
        "source_artifact_ids": ["PCM-RX-1"],
    }


def test_source_unavailable_is_explicit_not_render_failure():
    record = source_unavailable_audio_binding(finding_id="F-1", event_ref="EV-1")

    assert record["status"] == "FAILED"
    assert record["reason_code"] == "SOURCE_UNAVAILABLE"
    assert record["source_available"] is False

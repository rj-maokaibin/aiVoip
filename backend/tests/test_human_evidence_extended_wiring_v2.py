from __future__ import annotations

from types import SimpleNamespace

from app.services.evidence_report_source_artifacts import _prefer_human_visuals, _presentation_images
from app.services.human_evidence_extended_artifacts import _select_dtmf_event


class _Artifact:
    def __init__(self, artifact_id: str, atype: str, filename: str, *, priority: int = 0, kind: str | None = None, ready: bool = True):
        self.id = artifact_id
        self.type = atype
        self.filename = filename
        self.metadata_json = {}
        if kind:
            self.metadata_json = {
                "renderer_family": "HUMAN",
                "presentation_priority": priority,
                "visual_kind": kind,
                "annotation_complete": ready,
                "human_explanation": {
                    "what_to_look_at": "怎么看",
                    "observations": ["事实"],
                    "meaning": "意味着什么",
                    "evidence_boundary": "证据边界",
                    "plain_language_summary": "一句话结论",
                    "diagnostic_authority": "NONE",
                },
            }


def test_extended_projection_deduplicates_same_human_kind_by_priority_and_keeps_top_three():
    old_spectrum = _Artifact("old", "SPECTRUM_PNG", "human_old_spectrum.png", priority=100, kind="SPECTRUM")
    aligned_spectrum = _Artifact("aligned", "SPECTRUM_PNG", "00_aligned.png", priority=290, kind="SPECTRUM")
    multitrack = _Artifact("multi", "WAVEFORM_PNG", "00_multitrack.png", priority=310, kind="MULTI_TRACK")
    spectrogram = _Artifact("spec", "SPECTROGRAM_PNG", "human_spec.png", priority=100, kind="SPECTROGRAM")
    cross_layer = _Artifact("cross", "WAVEFORM_PNG", "01_cross.png", priority=90, kind="CROSS_LAYER")
    machine_wave = _Artifact("machine", "WAVEFORM_PNG", "machine_wave.png")

    projected = _prefer_human_visuals([old_spectrum, aligned_spectrum, multitrack, spectrogram, cross_layer, machine_wave])
    ids = {x.id for x in projected}
    assert "aligned" in ids
    assert "old" not in ids
    assert "machine" not in ids
    visible = _presentation_images(projected, limit=3)
    assert visible == {"multi", "aligned", "spec"}
    assert "cross" not in visible


def test_extended_projection_fails_closed_and_retains_machine_when_human_annotation_incomplete():
    incomplete = _Artifact("human", "RTP_TIMELINE_PNG", "human.png", priority=330, kind="RTP_TIMELINE", ready=False)
    machine = _Artifact("machine", "RTP_TIMELINE_PNG", "machine.png")
    projected = _prefer_human_visuals([machine, incomplete])
    assert machine in projected
    assert incomplete in projected
    assert _presentation_images(projected, limit=3) == {"human", "machine"}


def test_dtmf_wiring_uses_authoritative_quality_event_index_before_nearest_time():
    session = {
        "start_time": 100.0,
        "dtmf_events": [
            {"digit": "6", "start_seconds": 1.0, "end_seconds": 1.1, "confidence": 0.9},
            {"digit": "0", "start_seconds": 1.3, "end_seconds": 1.4, "confidence": 0.9},
            {"digit": "1", "start_seconds": 1.6, "end_seconds": 1.7, "confidence": 0.9},
        ],
    }
    finding = SimpleNamespace(metrics_json={"event_index": 1, "digit": "0"}, representative_time=101.02)
    event = _select_dtmf_event(session, finding, "601")
    assert event["digit"] == "0"


def test_dtmf_wiring_uses_nearest_canonical_event_when_no_quality_index():
    session = {
        "start_time": 100.0,
        "dtmf_events": [
            {"digit": "6", "start_seconds": 1.0, "end_seconds": 1.1, "confidence": 0.9},
            {"digit": "0", "start_seconds": 1.3, "end_seconds": 1.4, "confidence": 0.9},
        ],
    }
    finding = SimpleNamespace(metrics_json={}, representative_time=101.31)
    event = _select_dtmf_event(session, finding, None)
    assert event["digit"] == "0"

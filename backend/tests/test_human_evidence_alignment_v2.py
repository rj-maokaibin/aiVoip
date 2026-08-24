from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import numpy as np

from app.reports.human_visuals.explanations import build_human_explanation
from app.reports.human_visuals.periodic_measurements import periodic_measurement
from app.reports.human_visuals.wav_window import slice_pcm16_wav_bytes
from app.services.evidence_report_source_artifacts import _human_visual_ready_meta, _prefer_human_visuals, _projection_metadata


class _Artifact:
    def __init__(self, artifact_id: str, atype: str, meta: dict | None = None):
        self.id = artifact_id
        self.type = atype
        self.metadata_json = meta or {}


def _wav(seconds: float = 2.0, sample_rate: int = 8000) -> bytes:
    t = np.arange(int(seconds * sample_rate), dtype=np.float64) / sample_rate
    samples = np.clip(5000 * np.sin(2 * np.pi * 150 * t), -32768, 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sample_rate); wf.writeframes(samples.astype("<i2").tobytes())
    return out.getvalue()


def _ready_meta() -> dict:
    return {
        "renderer_family": "HUMAN",
        "annotation_complete": True,
        "visual_kind": "SPECTRUM",
        "human_explanation": {
            "what_to_look_at": "看频率成分。",
            "observations": ["检测到周期性峰值。"],
            "meaning": "支持当前 Finding 的周期性信号证据。",
            "evidence_boundary": "不能单独确认物理根因。",
            "plain_language_summary": "存在周期性信号特征。",
            "diagnostic_authority": "NONE",
        },
        "annotation_contract": {"title": "周期性干扰频谱"},
    }


def test_human_visual_readiness_fails_closed_when_explanation_is_incomplete():
    incomplete = _ready_meta()
    incomplete["human_explanation"] = {"what_to_look_at": "看频率。"}
    assert _human_visual_ready_meta(incomplete) is False
    machine = _Artifact("m", "SPECTRUM_PNG", {"renderer_family": "MACHINE"})
    human = _Artifact("h", "SPECTRUM_PNG", incomplete)
    projected = _prefer_human_visuals([machine, human])
    assert machine in projected
    assert human in projected


def test_ready_human_visual_suppresses_same_type_machine_only_in_projection():
    machine = _Artifact("m", "SPECTRUM_PNG", {"renderer_family": "MACHINE"})
    human = _Artifact("h", "SPECTRUM_PNG", _ready_meta())
    projected = _prefer_human_visuals([machine, human])
    assert human in projected
    assert machine not in projected
    meta = _projection_metadata(human)
    assert meta["human_visual_ready"] is True
    assert meta["annotation_contract"]["human_explanation"]["evidence_boundary"]
    assert meta["annotation_contract"]["human_explanation_rendered"] == "STRUCTURED_POST_IMAGE_V2"


def test_periodic_measurement_and_explanation_use_canonical_numeric_facts_without_diagnosis():
    metrics = {
        "details": {
            "pcm_rx": {
                "level": "HIGH",
                "estimated_period": {"period_ms": 20.0, "frequency_hz": 50.0, "correlation": 0.93},
                "representative": {
                    "rms_dbfs": -45.445,
                    "duration_seconds": 1.0,
                    "autocorrelation": {"10ms": -0.7, "20ms": 0.92, "40ms": 0.88},
                    "periodicity_score": 0.83,
                },
                "comb": {
                    "detected": True,
                    "hit_count": 5,
                    "target_count": 5,
                    "members": [
                        {"target_hz": 150, "peak_hz": 150.0, "hit": True},
                        {"target_hz": 250, "peak_hz": 250.0, "hit": True},
                        {"target_hz": 350, "peak_hz": 350.0, "hit": True},
                        {"target_hz": 450, "peak_hz": 450.0, "hit": True},
                        {"target_hz": 550, "peak_hz": 550.0, "hit": True},
                    ],
                },
            },
            "evidence_boundary": "周期/谐波结构属于直接信号证据；具体硬件根因仍需A/B实验确认。",
        }
    }
    periodic = periodic_measurement(metrics)
    finding = SimpleNamespace(
        observation="PCM RX 中检测到稳定周期特征。",
        interpretation="当前证据支持数字音频中存在周期性干扰。",
        root_cause_boundary="不能单独确认电源、接地、话柄或SLIC根因。",
        title="周期性音频干扰",
    )
    explanation = build_human_explanation(
        finding,
        "SPECTRUM",
        measurement={"periodic": periodic, "evidence_source_strategy": "PERIODIC_AUDIO_CLIP"},
    )
    text = " ".join(explanation["observations"])
    assert "20" in text and "50" in text
    assert "-45.445 dBFS" in text
    assert "150" in text and "550" in text
    assert "代表性周期干扰音频片段" in text
    assert explanation["diagnostic_authority"] == "NONE"
    assert explanation["measurement_authority"] == "PRESENTATION_FACT_ONLY"
    assert explanation["evidence_boundary"] == finding.root_cause_boundary


def test_pcm16_wav_window_preserves_format_and_exact_duration():
    source = _wav(2.0)
    clipped, meta = slice_pcm16_wav_bytes(source, 0.5, 1.5)
    with wave.open(io.BytesIO(clipped), "rb") as wf:
        assert wf.getframerate() == 8000
        assert wf.getsampwidth() == 2
        assert wf.getnchannels() == 1
        assert wf.getnframes() == 8000
    assert meta["source_window_seconds"] == [0.5, 1.5]
    assert meta["output_duration_seconds"] == 1.0

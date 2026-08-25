from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import numpy as np

from app.reports.human_visuals.explanations import build_human_explanation
from app.reports.human_visuals.periodic_measurements import periodic_measurement
from app.reports.human_visuals.wav_window import slice_pcm16_wav_bytes
from app.services.evidence_report_artifacts import _periodic_sources
from app.services.evidence_report_source_artifacts import _human_visual_ready_meta, _prefer_human_visuals, _projection_metadata


class _Artifact:
    def __init__(self, artifact_id: str, atype: str, meta: dict | None = None, object_key: str | None = None):
        self.id = artifact_id
        self.type = atype
        self.metadata_json = meta or {}
        self.object_key = object_key or artifact_id
        self.analyzer_run_id = "run"
        self.evidence_id = "evidence"


class _Storage:
    def __init__(self, values: dict[str, bytes]):
        self.values = values

    def get_bytes(self, key: str) -> bytes:
        return self.values[key]


def _wav(seconds: float = 2.0, sample_rate: int = 8000) -> bytes:
    t = np.arange(int(seconds * sample_rate), dtype=np.float64) / sample_rate
    samples = np.clip(5000 * np.sin(2 * np.pi * 150 * t), -32768, 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sample_rate); wf.writeframes(samples.astype("<i2").tobytes())
    return out.getvalue()


def _periodic_metrics() -> dict:
    return {
        "details": {
            "pcm_rx": {
                "level": "HIGH",
                "estimated_period": {"period_ms": 20.0, "frequency_hz": 50.0, "correlation": 0.93},
                "representative": {
                    "rms_dbfs": -45.445, "duration_seconds": 1.0, "start_seconds": 0.5,
                    "autocorrelation": {"10ms": -0.7, "20ms": 0.92, "40ms": 0.88}, "periodicity_score": 0.83,
                },
                "comb": {
                    "detected": True, "hit_count": 5, "target_count": 5,
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


def _ready_meta() -> dict:
    return {
        "renderer_family": "HUMAN", "annotation_complete": True, "visual_kind": "SPECTRUM",
        "human_explanation": {
            "what_to_look_at": "看频率成分。", "observations": ["检测到周期性峰值。"],
            "meaning": "支持当前 Finding 的周期性信号证据。", "evidence_boundary": "不能单独确认物理根因。",
            "plain_language_summary": "存在周期性信号特征。", "diagnostic_authority": "NONE",
        },
        "annotation_contract": {"title": "周期性干扰频谱"},
    }


def test_human_visual_readiness_fails_closed_when_explanation_is_incomplete():
    incomplete = _ready_meta(); incomplete["human_explanation"] = {"what_to_look_at": "看频率。"}
    assert _human_visual_ready_meta(incomplete) is False
    machine = _Artifact("m", "SPECTRUM_PNG", {"renderer_family": "MACHINE"}); human = _Artifact("h", "SPECTRUM_PNG", incomplete)
    projected = _prefer_human_visuals([machine, human])
    assert machine in projected and human not in projected


def test_ready_human_visual_suppresses_same_type_machine_only_in_projection():
    machine = _Artifact("m", "SPECTRUM_PNG", {"renderer_family": "MACHINE"}); human = _Artifact("h", "SPECTRUM_PNG", _ready_meta())
    projected = _prefer_human_visuals([machine, human])
    assert human in projected and machine not in projected
    meta = _projection_metadata(human)
    assert meta["human_visual_ready"] is True
    assert meta["annotation_contract"]["human_explanation"]["evidence_boundary"]
    assert meta["annotation_contract"]["human_explanation_rendered"] == "STRUCTURED_POST_IMAGE_V2"


def test_periodic_measurement_and_explanation_use_canonical_numeric_facts_without_diagnosis():
    periodic = periodic_measurement(_periodic_metrics())
    finding = SimpleNamespace(
        observation="PCM RX 中检测到稳定周期特征。", interpretation="当前证据支持数字音频中存在周期性干扰。",
        root_cause_boundary="不能单独确认电源、接地、话柄或SLIC根因。", title="周期性音频干扰",
    )
    explanation = build_human_explanation(finding, "SPECTRUM", measurement={"periodic": periodic, "evidence_source_strategy": "PERIODIC_AUDIO_CLIP"})
    text = " ".join(explanation["observations"])
    assert "20" in text and "50" in text and "-45.445 dBFS" in text
    assert "150" in text and "550" in text and "代表性周期干扰音频片段" in text
    assert explanation["diagnostic_authority"] == "NONE" and explanation["measurement_authority"] == "PRESENTATION_FACT_ONLY"
    assert explanation["evidence_boundary"] == finding.root_cause_boundary


def test_pcm16_wav_window_preserves_format_and_exact_duration():
    clipped, meta = slice_pcm16_wav_bytes(_wav(2.0), 0.5, 1.5)
    with wave.open(io.BytesIO(clipped), "rb") as wf:
        assert wf.getframerate() == 8000 and wf.getsampwidth() == 2 and wf.getnchannels() == 1 and wf.getnframes() == 8000
    assert meta["source_window_seconds"] == [0.5, 1.5] and meta["output_duration_seconds"] == 1.0


def test_periodic_sources_prefer_analyzer_representative_audio_clip():
    full = _wav(2.0); clip = _wav(1.0); metrics = _periodic_metrics()
    scope = {"pcm_tap": "pcm_rx", "pcm_session_index": 0, "call_id": "call-1"}
    finding = SimpleNamespace(scope_json=scope, start_time=100.5, end_time=101.5, representative_time=100.5, metrics_json={})
    session = {"start_time": 100.0, "end_time": 102.0}
    wav = _Artifact("wav", "PCM_WAV", {"pcm_tap": "pcm_rx", "session_index": 0}, "full.wav")
    clip_art = _Artifact("clip", "PERIODIC_AUDIO_CLIP", {"event_type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "source": "pcm_rx", "scope": scope}, "clip.wav")
    metrics_art = _Artifact("metrics", "PERIODIC_METRICS_JSON", {"event_type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "scope": scope}, "metrics.json")
    storage = _Storage({"full.wav": full, "clip.wav": clip, "metrics.json": json.dumps(metrics).encode()})
    source, data, periodic, _, strategy = _periodic_sources(storage, finding=finding, session=session, wav=wav, clips=[clip_art], metrics_rows=[metrics_art])
    assert source is clip_art and data == clip and strategy == "PERIODIC_AUDIO_CLIP"
    assert periodic["period_ms"] == 20.0 and periodic["comb_hit_count"] == 5


def test_periodic_sources_fallback_to_finding_window_when_clip_missing():
    full = _wav(2.0); scope = {"pcm_tap": "pcm_rx", "pcm_session_index": 0}
    finding = SimpleNamespace(scope_json=scope, start_time=100.5, end_time=101.5, representative_time=100.5, metrics_json={})
    session = {"start_time": 100.0, "end_time": 102.0}
    wav = _Artifact("wav", "PCM_WAV", {"pcm_tap": "pcm_rx", "session_index": 0}, "full.wav")
    source, data, _, _, strategy = _periodic_sources(_Storage({"full.wav": full}), finding=finding, session=session, wav=wav, clips=[], metrics_rows=[])
    assert source is wav and strategy == "PCM_WAV_FINDING_WINDOW"
    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnframes() == 8000

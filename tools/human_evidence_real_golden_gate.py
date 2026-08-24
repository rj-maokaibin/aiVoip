#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.reports.human_visuals import (
    build_human_explanation,
    render_human_spectrum_png_from_wav,
    render_human_spectrogram_png_from_wav,
)
from app.reports.human_visuals.periodic_measurements import merge_visual_measurement, periodic_measurement

PNG = b"\x89PNG\r\n\x1a\n"


def _require(path: Path) -> Path:
    if not path.is_file():
        raise AssertionError(f"HUMAN_REAL_GOLDEN_MISSING:{path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Real Offline Golden #001 Human Evidence gate")
    parser.add_argument("--artifacts", type=Path, default=Path("offline-golden-artifacts"))
    parser.add_argument("--output", type=Path, default=Path("offline-golden-human-artifacts"))
    args = parser.parse_args()

    root = args.artifacts
    wav_path = _require(root / "periodic_00_pcm_rx.wav")
    metrics_path = _require(root / "periodic_00_metrics.json")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    periodic = periodic_measurement(metrics, source="pcm_rx")

    assert periodic["period_ms"] is not None and 19.0 <= periodic["period_ms"] <= 21.0, periodic
    assert periodic["frequency_hz"] is not None and 47.0 <= periodic["frequency_hz"] <= 53.0, periodic
    assert periodic["comb_detected"] is True, periodic
    assert periodic["comb_hit_count"] >= 5, periodic
    assert len(periodic["harmonics_hz"]) >= 5, periodic
    assert periodic["rms_dbfs"] is not None, periodic

    wav = wav_path.read_bytes()
    refs = periodic["harmonics_hz"]
    spectrum_png, spectrum_measurement = render_human_spectrum_png_from_wav(
        wav,
        canonical_spectral={},
        reference_frequencies_hz=refs,
        title="周期性音频干扰 · Continuous Spectrum",
        subtitle="PCM RX · Real Offline Golden #001 · 代表性证据片段",
        max_frequency_hz=1200.0,
        max_seconds=2.0,
    )
    spectrogram_png, spectrogram_measurement = render_human_spectrogram_png_from_wav(
        wav,
        start_seconds=0.0,
        end_seconds=None,
        max_frequency_hz=1200.0,
        reference_frequencies_hz=refs,
        title="周期性音频干扰 · High Resolution Spectrogram",
        subtitle="PCM RX · Real Offline Golden #001 · 代表性证据片段",
    )
    assert spectrum_png.startswith(PNG)
    assert spectrogram_png.startswith(PNG)
    assert spectrum_measurement["measurement_method"] == "NUMPY_RFFT_HANN_COHERENT_GAIN_V1"
    assert spectrum_measurement["level_unit"] == "dBFS"
    assert spectrogram_measurement["measurement_method"] == "NUMPY_STFT_HANN_RELATIVE_DB_V1"
    assert spectrogram_measurement["level_unit"] == "relative dB"
    assert spectrogram_measurement["absolute_dbfs"] is False

    finding = SimpleNamespace(
        observation="PCM RX 中检测到稳定约20ms周期特征。",
        interpretation=(metrics.get("details") or {}).get("interpretation") or "当前证据支持数字音频中存在周期性干扰。",
        root_cause_boundary=(metrics.get("details") or {}).get("evidence_boundary") or "不能单独确认具体物理根因。",
        title="周期性音频干扰",
    )
    measurement = merge_visual_measurement(
        spectrum_measurement,
        periodic,
        evidence_source_strategy="PERIODIC_AUDIO_CLIP",
        time_window_seconds=[0.0, periodic.get("representative_duration_seconds") or 1.0],
    )
    explanation = build_human_explanation(finding, "SPECTRUM", measurement=measurement)
    observations = " ".join(explanation["observations"])
    assert "20" in observations and "Hz" in observations, explanation
    assert "梳状谱" in observations, explanation
    assert explanation["diagnostic_authority"] == "NONE"
    assert explanation["measurement_authority"] == "PRESENTATION_FACT_ONLY"

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pcm_rx_periodic_spectrum_human_v2.png").write_bytes(spectrum_png)
    (args.output / "pcm_rx_periodic_spectrogram_human_v2.png").write_bytes(spectrogram_png)
    result = {
        "schema_version": "human-evidence-real-golden-gate-v1",
        "status": "PASS",
        "source": "Real Offline Golden #001",
        "source_strategy": "PERIODIC_AUDIO_CLIP",
        "periodic": periodic,
        "spectrum_measurement": spectrum_measurement,
        "spectrogram_measurement": spectrogram_measurement,
        "human_explanation": explanation,
    }
    (args.output / "human-golden-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("HUMAN_REAL_OFFLINE_GOLDEN_001=PASS")
    print(json.dumps({"period_ms": periodic["period_ms"], "frequency_hz": periodic["frequency_hz"], "comb_hits": periodic["comb_hit_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

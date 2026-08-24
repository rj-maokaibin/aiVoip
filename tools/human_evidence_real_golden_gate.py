#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.analyzers.pcm.signal import detect_dtmf, dtmf_sequence
from app.reports.human_visuals import (
    build_human_explanation,
    render_human_dtmf_inspector_png,
    render_human_spectrum_png_from_wav,
    render_human_spectrogram_png_from_wav,
)
from app.reports.human_visuals.periodic_measurements import merge_visual_measurement, periodic_measurement

PNG = b"\x89PNG\r\n\x1a\n"
DEFAULT_MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"


def _require(path: Path) -> Path:
    if not path.is_file():
        raise AssertionError(f"HUMAN_REAL_GOLDEN_MISSING:{path}")
    return path


def _read_pcm16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        assert wf.getsampwidth() == 2, wf.getsampwidth()
        sample_rate = int(wf.getframerate())
        channels = int(wf.getnchannels())
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").copy()
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples, sample_rate


def main() -> int:
    parser = argparse.ArgumentParser(description="Real Offline Golden #001 Human Evidence gate")
    parser.add_argument("--artifacts", type=Path, default=Path("offline-golden-artifacts"))
    parser.add_argument("--output", type=Path, default=Path("offline-golden-human-artifacts"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    root = args.artifacts
    wav_path = _require(root / "periodic_00_pcm_rx.wav")
    pcm_rx_path = _require(root / "pcm_pcm_rx_00.wav")
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

    # Golden-only DTMF verification: expected values come from the reviewed
    # manifest and are never injected into production Analyzer/Composer logic.
    dtmf_expected = (manifest.get("expected") or {}).get("dtmf") or {}
    expected_digits = str(dtmf_expected.get("pcm_digits") or "")
    expected_target = str(dtmf_expected.get("sip_target") or "")
    samples, sample_rate = _read_pcm16(pcm_rx_path)
    dtmf_events = detect_dtmf(samples, sample_rate)
    sequences = dtmf_sequence(dtmf_events)
    detected_sequences = [str(x.get("digits") or "") for x in sequences]
    assert expected_digits and expected_digits in detected_sequences, {"expected": expected_digits, "actual": detected_sequences}
    first_digit = expected_digits[0]
    first_event = next((x for x in dtmf_events if str(x.get("digit") or "") == first_digit), None)
    assert first_event is not None, {"first_digit": first_digit, "events": dtmf_events}
    dtmf_png, dtmf_measurement = render_human_dtmf_inspector_png(
        pcm_rx_path.read_bytes(),
        first_event,
        sip_target=expected_target,
        pcm_sequence=expected_digits,
        title=f"DTMF {first_digit} · Real Offline Golden #001",
    )
    assert dtmf_png.startswith(PNG)
    assert dtmf_measurement["digit"] == first_digit
    assert dtmf_measurement["sequence_match"] == "MATCH"
    assert dtmf_measurement["pcm_sequence"] == expected_digits
    assert dtmf_measurement["sip_target"] == expected_target
    assert dtmf_measurement["threshold_status"] == "UNVERIFIED_THRESHOLD"
    assert dtmf_measurement["measurement_method"] == "DTMF_EVENT_RFFT_HANN_FINE_PEAK_V1"

    dtmf_finding = SimpleNamespace(
        observation=f"PCM RX 已接受 DTMF {first_digit} 事件；Golden 序列为 {expected_digits}。",
        interpretation="当前 Inspector 只解释已接受事件的精细频域测量，并复核 PCM 序列与 SIP 目标的一致性。",
        root_cause_boundary="DTMF Inspector 不重新判定用户意图，也不在未冻结阈值时给频偏/杂散项目判 PASS/FAIL。",
        title="DTMF Inspector Golden",
    )
    dtmf_explanation = build_human_explanation(dtmf_finding, "DTMF_INSPECTOR", measurement=dtmf_measurement)
    dtmf_text = " ".join(dtmf_explanation["observations"])
    assert expected_digits in dtmf_text and expected_target in dtmf_text, dtmf_explanation
    assert "不判 PASS/FAIL" in dtmf_text, dtmf_explanation
    assert dtmf_explanation["diagnostic_authority"] == "NONE"

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pcm_rx_periodic_spectrum_human_v2.png").write_bytes(spectrum_png)
    (args.output / "pcm_rx_periodic_spectrogram_human_v2.png").write_bytes(spectrogram_png)
    (args.output / "pcm_rx_dtmf_first_digit_inspector_human_v2.png").write_bytes(dtmf_png)
    result = {
        "schema_version": "human-evidence-real-golden-gate-v2",
        "status": "PASS",
        "source": "Real Offline Golden #001",
        "source_strategy": "PERIODIC_AUDIO_CLIP",
        "periodic": periodic,
        "spectrum_measurement": spectrum_measurement,
        "spectrogram_measurement": spectrogram_measurement,
        "human_explanation": explanation,
        "dtmf": {
            "expected_digits": expected_digits,
            "expected_sip_target": expected_target,
            "detected_sequences": detected_sequences,
            "inspected_digit": first_digit,
            "measurement": dtmf_measurement,
            "human_explanation": dtmf_explanation,
        },
    }
    (args.output / "human-golden-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("HUMAN_REAL_OFFLINE_GOLDEN_001=PASS")
    print(json.dumps({
        "period_ms": periodic["period_ms"],
        "frequency_hz": periodic["frequency_hz"],
        "comb_hits": periodic["comb_hit_count"],
        "dtmf_sequence": expected_digits,
        "dtmf_inspected_digit": first_digit,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

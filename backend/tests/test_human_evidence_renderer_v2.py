from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import numpy as np

from app.reports.human_visuals.explanations import build_human_explanation
from app.reports.human_visuals.renderers import (
    render_human_spectrum_png_from_wav,
    render_human_spectrogram_png,
    render_human_waveform_png,
)
from app.reports.human_visuals.wav_spectrogram import render_human_spectrogram_png_from_wav
from app.services.evidence_report_source_artifacts import _prefer_human_visuals, _projection_metadata

PNG = b"\x89PNG\r\n\x1a\n"


def _wav_bytes(samples: np.ndarray, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1);wf.setsampwidth(2);wf.setframerate(sample_rate);wf.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def _tone_wav() -> bytes:
    sr=8000;t=np.arange(sr*2,dtype=np.float64)/sr
    x=9000*np.sin(2*np.pi*150*t)+5000*np.sin(2*np.pi*250*t)
    return _wav_bytes(np.clip(x,-32768,32767).astype(np.int16),sr)


def test_human_spectrum_is_continuous_dbfs_measurement():
    png,meta=render_human_spectrum_png_from_wav(_tone_wav(),canonical_spectral={"peaks":[{"frequency_hz":150.0,"energy_ratio":0.2},{"frequency_hz":250.0,"energy_ratio":0.1}]},reference_frequencies_hz=[50,150,250,350],title="Periodic spectrum")
    assert png.startswith(PNG);assert meta["measurement_method"]=="NUMPY_RFFT_HANN_COHERENT_GAIN_V1";assert meta["level_unit"]=="dBFS"
    assert meta["amplitude_reference"]=="PCM16_FULL_SCALE_32768";assert meta["sample_rate"]==8000;assert meta["fft_size"]>=256


def test_human_high_resolution_spectrogram_is_wav_grounded_relative_measurement():
    png,meta=render_human_spectrogram_png_from_wav(_tone_wav(),start_seconds=0.25,end_seconds=1.25,max_frequency_hz=1200.0,reference_frequencies_hz=[150,250,350],title="Periodic spectrogram")
    assert png.startswith(PNG);assert meta["measurement_method"]=="NUMPY_STFT_HANN_RELATIVE_DB_V1";assert meta["sample_rate"]==8000
    assert meta["level_unit"]=="relative dB";assert meta["absolute_dbfs"] is False;assert meta["frequency_range_hz"]==[0.0,1200.0];assert meta["time_window_seconds"]==[0.25,1.25]


def test_human_waveform_and_legacy_spectrogram_generate_png_without_new_diagnosis():
    waveform={"duration_seconds":1.0,"bins":[{"t":0.0,"min":-1000,"max":1000,"rms_dbfs":-30.0},{"t":0.5,"min":-15000,"max":16000,"rms_dbfs":-12.0},{"t":0.99,"min":-500,"max":500,"rms_dbfs":-36.0}]}
    spec={"times":[0.0,0.5,1.0],"frequencies":[0.0,500.0,1000.0,2000.0,4000.0],"db":[[10.0,20.0,30.0,10.0,0.0],[12.0,25.0,35.0,12.0,1.0],[9.0,19.0,28.0,9.0,0.0]]}
    assert render_human_waveform_png(waveform,anomaly_start=0.45,anomaly_end=0.60).startswith(PNG)
    assert render_human_spectrogram_png(spec,anomaly_start=0.45,anomaly_end=0.60).startswith(PNG)


def test_human_explanation_copies_canonical_finding_and_has_no_diagnostic_authority():
    finding=SimpleNamespace(observation="PCM RX 中检测到稳定约20ms周期特征。",interpretation="当前证据支持数字音频中存在周期性干扰。",root_cause_boundary="不能单独确认电源、接地、话柄或SLIC根因。",title="周期性音频干扰")
    value=build_human_explanation(finding,"SPECTRUM")
    assert value["observations"]==[finding.observation];assert value["meaning"]==finding.interpretation;assert value["evidence_boundary"]==finding.root_cause_boundary
    assert value["source_authority"]=="CANONICAL_FINDING";assert value["diagnostic_authority"]=="NONE"


class _Artifact:
    def __init__(self,artifact_id:str,atype:str,meta:dict|None=None):self.id=artifact_id;self.type=atype;self.metadata_json=meta or {}


def _human_meta()->dict:
    return {
        "renderer_family":"HUMAN","renderer_version":"human-evidence-renderer-v2","annotation_complete":True,"visual_kind":"SPECTRUM",
        "human_explanation":{
            "what_to_look_at":"看频率成分。","observations":["检测到150Hz和250Hz峰。"],"meaning":"支持周期性干扰证据。",
            "evidence_boundary":"不能单独确认物理根因。","plain_language_summary":"存在周期性音频干扰。","diagnostic_authority":"NONE",
        },
        "annotation_contract":{"caption":"original"},
    }


def test_human_visual_replaces_same_type_machine_only_in_presentation_projection():
    machine_spectrum=_Artifact("m1","SPECTRUM_PNG",{"renderer_version":"evidence-renderer-v2"});human_spectrum=_Artifact("h1","SPECTRUM_PNG",_human_meta());machine_wave=_Artifact("m2","WAVEFORM_PNG",{"renderer_version":"evidence-renderer-v2"})
    projected=_prefer_human_visuals([machine_spectrum,human_spectrum,machine_wave])
    assert human_spectrum in projected;assert machine_spectrum not in projected;assert machine_wave in projected


def test_human_projection_keeps_short_caption_and_structured_post_image_explanation():
    human=_Artifact("h1","SPECTRUM_PNG",_human_meta());meta=_projection_metadata(human);annotation=meta["annotation_contract"]
    assert annotation["caption"].startswith("SPECTRUM｜")
    assert annotation["human_explanation_rendered"]=="STRUCTURED_POST_IMAGE_V2"
    explanation=annotation["human_explanation"]
    assert explanation["what_to_look_at"]=="看频率成分。";assert explanation["meaning"]=="支持周期性干扰证据。"
    assert explanation["evidence_boundary"]=="不能单独确认物理根因。";assert explanation["plain_language_summary"]=="存在周期性音频干扰。"
    assert human.metadata_json["annotation_contract"]["caption"]=="original"

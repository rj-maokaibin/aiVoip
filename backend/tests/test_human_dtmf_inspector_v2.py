from __future__ import annotations

import io
import wave

import numpy as np

from app.reports.human_visuals.dtmf_inspector import measure_dtmf_event, render_human_dtmf_inspector_png

PNG=b"\x89PNG\r\n\x1a\n"


def _wav_digit_6(sample_rate:int=8000,duration:float=0.12)->bytes:
    t=np.arange(int(sample_rate*duration),dtype=np.float64)/sample_rate
    x=9000*np.sin(2*np.pi*770*t)+8000*np.sin(2*np.pi*1477*t)+400*np.sin(2*np.pi*1030*t)
    samples=np.clip(x,-32768,32767).astype(np.int16)
    out=io.BytesIO()
    with wave.open(out,"wb") as wf:
        wf.setnchannels(1);wf.setsampwidth(2);wf.setframerate(sample_rate);wf.writeframes(samples.astype("<i2").tobytes())
    return out.getvalue()


def test_dtmf_inspector_measures_expected_dual_tones_without_pass_fail_invention():
    event={"digit":"6","start_seconds":0.0,"end_seconds":0.12,"duration_ms":120.0,"confidence":0.95}
    result=measure_dtmf_event(_wav_digit_6(),event)
    assert result["status"]=="MEASURED"
    assert result["threshold_status"]=="UNVERIFIED_THRESHOLD"
    assert abs(result["row_measured_hz"]-770.0)<2.0
    assert abs(result["col_measured_hz"]-1477.0)<2.0
    assert result["row_level_dbfs"]<0 and result["col_level_dbfs"]<0
    assert result["strongest_spur_hz"] is not None
    assert result["spur_margin_db"] is not None
    assert result["authority"]=="PRESENTATION_MEASUREMENT_ONLY"
    assert "PASS" not in result and "FAIL" not in result


def test_dtmf_inspector_png_contains_sequence_comparison_as_measurement_only():
    event={"digit":"6","start_seconds":0.0,"end_seconds":0.12,"duration_ms":120.0,"confidence":0.95}
    png,result=render_human_dtmf_inspector_png(_wav_digit_6(),event,pcm_sequence="601",sip_target="601")
    assert png.startswith(PNG)
    assert result["sequence_match"]=="MATCH"
    assert result["pcm_sequence"]=="601" and result["sip_target"]=="601"
    assert result["threshold_status"]=="UNVERIFIED_THRESHOLD"

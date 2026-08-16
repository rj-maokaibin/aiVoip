import math
from pathlib import Path

import numpy as np
import pytest

from app.analyzers.attachments import analyze_field_audio
from app.api.v1.uploads import infer_type


def test_explicit_raw_pcm_format_is_analyzed(tmp_path: Path):
    rate = 8000
    samples = (np.sin(2 * math.pi * 440 * np.arange(rate) / rate) * 8000).astype("<i2")
    source = tmp_path / "field.pcm"
    source.write_bytes(samples.tobytes())
    result = analyze_field_audio(source, pcm_format={
        "sample_rate": rate, "sample_width_bits": 16, "channels": 1,
        "signed": True, "endian": "little",
    })
    assert result["summary"]["availability"] == "ANALYZED"
    assert result["summary"]["source_format"] == "RAW_PCM"
    assert result["summary"]["decoded_by"] == "explicit_pcm_format"


def test_raw_pcm_without_parameters_is_fail_closed(tmp_path: Path):
    source = tmp_path / "field.raw"
    source.write_bytes(b"\0" * 160)
    with pytest.raises(ValueError, match="RAW_PCM_FORMAT_REQUIRED"):
        analyze_field_audio(source)


def test_upload_type_inference_routes_multimodal_files():
    assert infer_type("voice.ogg") == "FIELD_AUDIO"
    assert infer_type("capture.pcm") == "FIELD_AUDIO_RAW"
    assert infer_type("screen.png") == "FIELD_IMAGE"

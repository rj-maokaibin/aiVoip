from __future__ import annotations

from pathlib import Path
import wave
import numpy as np


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int, channels: int = 1) -> None:
    data = samples.astype('<i2', copy=False).tobytes()
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data)

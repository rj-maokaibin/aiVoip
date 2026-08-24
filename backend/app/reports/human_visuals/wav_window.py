from __future__ import annotations

import io
import wave


def slice_pcm16_wav_bytes(wav_bytes: bytes, start_seconds: float, end_seconds: float) -> tuple[bytes, dict]:
    """Return a PCM16 WAV sub-window without changing sample values."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = int(wf.getnchannels())
        sample_width = int(wf.getsampwidth())
        sample_rate = int(wf.getframerate())
        total_frames = int(wf.getnframes())
        compression = wf.getcomptype()
        if sample_width != 2 or compression != "NONE":
            raise ValueError("HUMAN_WAV_WINDOW_REQUIRES_PCM16")
        duration = total_frames / max(1, sample_rate)
        lo = max(0.0, min(duration, float(start_seconds)))
        hi = max(lo, min(duration, float(end_seconds)))
        if hi <= lo:
            hi = min(duration, lo + max(1.0 / sample_rate, 0.001))
        first = max(0, min(total_frames, int(round(lo * sample_rate))))
        last = max(first, min(total_frames, int(round(hi * sample_rate))))
        wf.setpos(first)
        frames = wf.readframes(last - first)

    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(sample_rate)
        target.writeframes(frames)
    return out.getvalue(), {
        "sample_rate": sample_rate,
        "channels": channels,
        "source_duration_seconds": round(duration, 6),
        "source_window_seconds": [round(lo, 6), round(hi, 6)],
        "output_duration_seconds": round((last - first) / max(1, sample_rate), 6),
        "method": "PCM16_WAV_EXACT_WINDOW_V1",
    }

from __future__ import annotations

from dataclasses import dataclass, asdict
from collections import defaultdict
import math
import numpy as np

from .codec import get_codec
from ..packet.types import NormalizedPacket
from app.analyzers.profile import get_default_analyzer_profile


@dataclass(slots=True)
class RenderedRtpTrack:
    stream_id: str
    src_ip: str | None
    src_port: int | None
    dst_ip: str | None
    dst_port: int | None
    ssrc: int | None
    codec: str
    sample_rate: int
    channels: int
    start_time: float
    end_time: float
    samples: np.ndarray
    packet_count: int
    inserted_loss_samples: int
    missing_payload_packets: int
    sequence_first: int
    sequence_last: int

    def metadata(self) -> dict:
        data = asdict(self)
        data.pop('samples')
        data['duration_seconds'] = round(self.samples.size / self.sample_rate / self.channels, 6)
        data['energy_timeline'] = _energy_timeline(self.samples, self.sample_rate)
        return data


def _dbfs_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    x = samples.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(x * x)))
    return -120.0 if rms <= 0 else 20.0 * math.log10(rms / 32768.0)


def _energy_timeline(samples: np.ndarray, sample_rate: int, frame_ms: int = 100) -> dict:
    """Return compact deterministic RTP window energy for cross-layer gating.

    This is evidence metadata, not a VAD classifier. CandidateDecision uses the
    adaptive threshold only to prove that the corresponding RTP window contains
    positive media energy before promoting a PCM silence mismatch.
    """
    frame = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    windows = []
    levels = []
    for start in range(0, samples.size, frame):
        end = min(samples.size, start + frame)
        level = _dbfs_rms(samples[start:end])
        levels.append(level)
        windows.append({
            'start_seconds': round(start / sample_rate, 6),
            'end_seconds': round(end / sample_rate, 6),
            'rms_dbfs': round(level, 3),
        })
    if not levels:
        threshold = -42.0
    else:
        cfg = get_default_analyzer_profile().section('silence')
        finite = np.asarray(levels, dtype=float)
        floor = float(np.percentile(finite, float(cfg['noise_floor_percentile'])))
        speech = float(np.percentile(finite, float(cfg['speech_percentile'])))
        threshold = min(
            float(cfg['threshold_max_dbfs']),
            max(
                float(cfg['threshold_min_dbfs']),
                min(floor + float(cfg['noise_floor_margin_db']), speech - float(cfg['speech_margin_db'])),
            ),
        )
    return {
        'frame_ms': frame_ms,
        'threshold_dbfs': round(float(threshold), 3),
        'windows': windows,
    }


def render_rtp_tracks(packets: list[NormalizedPacket], stream_results: list[dict]) -> list[RenderedRtpTrack]:
    by_key: dict[tuple, list[NormalizedPacket]] = defaultdict(list)
    for p in packets:
        if p.rtp and p.rtp.sequence is not None:
            by_key[(p.src_ip, p.src_port, p.dst_ip, p.dst_port, p.rtp.ssrc)].append(p)
    result_by_id = {x['stream_id']: x for x in stream_results}
    tracks: list[RenderedRtpTrack] = []
    for key, group in by_key.items():
        stream_id = _stream_id(key)
        sr = result_by_id.get(stream_id)
        if not sr:
            continue
        codec = get_codec(sr.get('codec'))
        if codec is None:
            continue
        track = _render_one(stream_id, key, sorted(group, key=lambda p: (p.timestamp, p.frame_number)), codec, sr)
        if track:
            tracks.append(track)
    tracks.sort(key=lambda x: x.start_time)
    return tracks


def _render_one(stream_id, key, packets, codec, stream_result) -> RenderedRtpTrack | None:
    src_ip, src_port, dst_ip, dst_port, ssrc = key
    unique: dict[int, NormalizedPacket] = {}
    reference = None
    for p in packets:
        seq = p.rtp.sequence & 0xFFFF
        ext = _extend_mod(seq, reference, 1 << 16)
        reference = ext if reference is None else max(reference, ext)
        unique.setdefault(ext, p)
    if not unique:
        return None
    seqs = sorted(unique)
    default_samples = _samples_per_packet(unique, codec.sample_rate, stream_result.get('ptime_ms'))
    parts: list[np.ndarray] = []
    inserted = 0
    missing_payload = 0
    for seq in range(seqs[0], seqs[-1] + 1):
        p = unique.get(seq)
        if p is None:
            parts.append(np.zeros(default_samples, dtype=np.int16)); inserted += default_samples
            continue
        payload_hex = p.rtp.payload_hex
        if not payload_hex:
            parts.append(np.zeros(default_samples, dtype=np.int16)); missing_payload += 1
            continue
        try:
            samples = codec.decode_i16(bytes.fromhex(payload_hex))
        except ValueError:
            samples = np.zeros(default_samples, dtype=np.int16); missing_payload += 1
        parts.append(samples)
    data = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    return RenderedRtpTrack(
        stream_id=stream_id, src_ip=src_ip, src_port=src_port, dst_ip=dst_ip, dst_port=dst_port, ssrc=ssrc,
        codec=codec.name, sample_rate=codec.sample_rate, channels=codec.channels,
        start_time=packets[0].timestamp, end_time=packets[-1].timestamp,
        samples=data, packet_count=len(packets), inserted_loss_samples=inserted,
        missing_payload_packets=missing_payload, sequence_first=seqs[0], sequence_last=seqs[-1],
    )


def _samples_per_packet(unique: dict[int, NormalizedPacket], sample_rate: int, ptime_ms: float | None) -> int:
    payload_sizes = []
    for p in unique.values():
        if p.rtp.payload_hex:
            payload_sizes.append(len(p.rtp.payload_hex) // 2)
    if payload_sizes:
        return max(1, int(np.median(payload_sizes)))
    if ptime_ms:
        return max(1, round(sample_rate * ptime_ms / 1000.0))
    return max(1, sample_rate // 50)


def _extend_mod(value: int, reference: int | None, modulus: int) -> int:
    if reference is None:
        return value
    base = reference - (reference % modulus)
    candidates = (base + value, base + value - modulus, base + value + modulus)
    return min(candidates, key=lambda x: abs(x - reference))


def _stream_id(key: tuple) -> str:
    src_ip, src_port, dst_ip, dst_port, ssrc = key
    return f'{src_ip}:{src_port}>{dst_ip}:{dst_port}/ssrc={ssrc}'

from __future__ import annotations

from typing import Iterable

from app.analyzers.audio.periodic import (
    analyze_low_energy_periodicity,
    periodic_strength,
    slice_by_absolute_time,
)
from app.analyzers.audio.rtp_audio import RenderedRtpTrack
from app.analyzers.profile import get_default_analyzer_profile


def _track_by_id(tracks: Iterable[RenderedRtpTrack]) -> dict[str, RenderedRtpTrack]:
    return {t.stream_id: t for t in tracks}


def _reverse_track(track: RenderedRtpTrack, tracks: Iterable[RenderedRtpTrack]) -> RenderedRtpTrack | None:
    candidates = [
        t for t in tracks
        if t.src_ip == track.dst_ip and t.src_port == track.dst_port
        and t.dst_ip == track.src_ip and t.dst_port == track.src_port
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: min(track.end_time, t.end_time) - max(track.start_time, t.start_time),
        reverse=True,
    )
    return candidates[0]


def _call_for_stream(packet_result: dict, stream_id: str) -> dict | None:
    for call in packet_result.get('calls', []) or []:
        if stream_id in (call.get('rtp_stream_ids') or []):
            return call
    return None


def _scope_window(pcm: dict, upstream: RenderedRtpTrack, downstream: RenderedRtpTrack | None, call: dict | None = None) -> tuple[float, float] | None:
    start = max(float(pcm['start_time']), float(upstream.start_time))
    end = min(float(pcm['end_time']), float(upstream.end_time))
    if downstream is not None:
        start = max(start, float(downstream.start_time))
        end = min(end, float(downstream.end_time))
    if call is not None and call.get('media_start_time') is not None:
        start = max(start, float(call['media_start_time']))
    if call is not None and call.get('media_end_time') is not None:
        end = min(end, float(call['media_end_time']))
    min_scope=float(get_default_analyzer_profile().section("periodic")["path"]["min_scope_seconds"])
    if end - start < min_scope:
        return None
    return start, end


def _analyze_signal(signal: dict, start: float, end: float) -> dict:
    samples = slice_by_absolute_time(signal['samples'], signal['sample_rate'], signal['start_time'], start, end)
    result = analyze_low_energy_periodicity(samples, signal['sample_rate'])
    result['analysis_window'] = {'start_time': start, 'end_time': end, 'duration_seconds': round(end-start, 6)}
    if result.get('representative'):
        rel = float(result['representative'].get('start_seconds', 0.0))
        result['representative']['absolute_start_time'] = round(start + rel, 6)
    return result


def _analyze_track(track: RenderedRtpTrack, start: float, end: float) -> dict:
    samples = slice_by_absolute_time(track.samples, track.sample_rate, track.start_time, start, end)
    result = analyze_low_energy_periodicity(samples, track.sample_rate)
    result['analysis_window'] = {'start_time': start, 'end_time': end, 'duration_seconds': round(end-start, 6)}
    if result.get('representative'):
        rel = float(result['representative'].get('start_seconds', 0.0))
        result['representative']['absolute_start_time'] = round(start + rel, 6)
    return result


def _representative_window(periodic: dict, fallback_start: float, fallback_end: float) -> tuple[float, float, float]:
    """Return the low-energy evidence window used for human review.

    The full active-media analysis interval remains available under details.*.analysis_window.
    Finding time instead points at the representative low-energy window so report/UI
    does not misrepresent a persistent periodic feature as a zero-duration instant.
    """
    rep = periodic.get('representative') or {}
    start = rep.get('absolute_start_time')
    duration = rep.get('duration_seconds')
    if start is None:
        start = fallback_start
    start = float(start)
    if duration is None:
        duration = min(1.0, max(0.0, fallback_end-start))
    end = min(float(fallback_end), start + max(0.001, float(duration)))
    if end <= start:
        end = min(float(fallback_end), start + 0.001)
    return start, end, start


def build_periodic_path_analysis(
    pcm_signals: list[dict],
    tracks: list[RenderedRtpTrack],
    correlations: list[dict],
    packet_result: dict,
) -> list[dict]:
    """Build deterministic local-capture periodic-interference evidence.

    Only pcm_rx mappings are used for the local capture direction. We intentionally select
    at most one best RTP mapping per PCM session to avoid duplicate hypotheses from the same
    audio interval. The opposite RTP direction is then used as a control.
    """
    path_cfg = get_default_analyzer_profile().section('periodic')['path']
    correlation_min = float(path_cfg['correlation_min'])
    downstream_margin = float(path_cfg['downstream_margin'])
    medium_pcm_strength = float(path_cfg['medium_pcm_strength'])
    medium_upstream_strength = float(path_cfg['medium_upstream_strength'])
    track_lookup = _track_by_id(tracks)
    pcm_lookup = {(p['tap']['name'], p['session_index']): p for p in pcm_signals}
    best: dict[tuple[str, int], dict] = {}
    for corr in correlations:
        details = corr.get('details') or {}
        if details.get('pcm_tap') != 'pcm_rx':
            continue
        quality = ((details.get('correlation') or {}).get('absolute_correlation')) or 0.0
        if float(quality) < correlation_min:
            continue
        key = ('pcm_rx', int(details.get('pcm_session_index', -1)))
        if key[1] < 0:
            continue
        if key not in best or float(quality) > float(((best[key].get('details') or {}).get('correlation') or {}).get('absolute_correlation', 0.0)):
            best[key] = corr

    out = []
    for (tap, session_index), corr in sorted(best.items(), key=lambda kv: kv[0][1]):
        pcm = pcm_lookup.get((tap, session_index))
        details = corr.get('details') or {}
        upstream = track_lookup.get(details.get('rtp_stream_id'))
        if pcm is None or upstream is None:
            continue
        downstream = _reverse_track(upstream, tracks)
        call = _call_for_stream(packet_result, upstream.stream_id)
        scope = _scope_window(pcm, upstream, downstream, call)
        if scope is None:
            continue
        start, end = scope
        pcm_periodic = _analyze_signal(pcm, start, end)
        up_periodic = _analyze_track(upstream, start, end)
        down_periodic = _analyze_track(downstream, start, end) if downstream is not None else None
        pcm_strength = periodic_strength(pcm_periodic)
        up_strength = periodic_strength(up_periodic)
        down_strength = periodic_strength(down_periodic)
        local_pattern = (
            pcm_periodic.get('level') == 'HIGH'
            and up_periodic.get('level') in {'HIGH', 'MEDIUM'}
            and (down_periodic is None or down_strength + downstream_margin < min(pcm_strength, up_strength))
        )
        level = 'HIGH' if local_pattern else 'MEDIUM' if pcm_strength >= medium_pcm_strength and up_strength >= medium_upstream_strength else 'LOW'
        event_type = 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE' if local_pattern else 'PERIODIC_INTERFERENCE_PATH_COMPARISON'
        call_id = call.get('call_id') if call else None
        evidence_start, evidence_end, representative_time = _representative_window(pcm_periodic, start, end)
        out.append({
            'type': event_type,
            'time': representative_time,
            'start_time': evidence_start,
            'end_time': evidence_end,
            'representative_time': representative_time,
            'severity': 'HIGH' if local_pattern else 'INFO',
            'scope': {
                'call_id': call_id,
                'pcm_tap': tap,
                'pcm_session_index': session_index,
                'upstream_rtp_stream_id': upstream.stream_id,
                'downstream_rtp_stream_id': downstream.stream_id if downstream else None,
                'active_media_window': {'start_time': start, 'end_time': end, 'duration_seconds': round(end-start, 6)},
                'representative_evidence_window': {'start_time': evidence_start, 'end_time': evidence_end, 'duration_seconds': round(evidence_end-evidence_start, 6)},
            },
            'details': {
                'level': level,
                'pcm_rx': pcm_periodic,
                'upstream_rtp': up_periodic,
                'downstream_rtp': down_periodic,
                'strength': {
                    'pcm_rx': round(pcm_strength, 6),
                    'upstream_rtp': round(up_strength, 6),
                    'downstream_rtp': round(down_strength, 6),
                },
                'correlation': details.get('correlation'),
                'interpretation': (
                    '低能量pcm_rx存在稳定约20ms周期和50Hz相关奇次谐波梳状谱；同类周期在APF上行RTP中仍明显，'
                    '而反向RTP未表现出同等级特征。该证据强支持异常在本地采集链路中已经形成，但不能单独区分电源/接地、话机/线路、FXS/SLIC或PCM接口。'
                    if local_pattern else
                    '已完成pcm_rx、上行RTP与反向RTP的周期特征比较，当前未满足“本地采集周期干扰”高置信门限。'
                ),
                'evidence_boundary': '周期/谐波结构属于直接信号证据；具体硬件根因仍需A/B实验确认。',
            },
        })
    return out
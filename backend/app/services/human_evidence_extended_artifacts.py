from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.evidence_report import EvidenceReportArtifactType
from app.db.evidence_report_models import EvidenceFinding, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact
from app.reports.evidence_visuals import visual_metadata
from app.reports.human_visuals import (
    HUMAN_RENDERER_VERSION,
    PRESENTATION_PROFILE,
    build_human_explanation,
    human_renderer_enabled,
    render_human_cross_layer_png,
    render_human_dtmf_inspector_png,
    render_human_multitrack_png,
    render_human_rtp_timeline_png,
    render_human_spectrum_png_from_wav,
)
from app.reports.human_visuals.periodic_measurements import merge_visual_measurement, periodic_measurement
from app.services.audit import audit
from app.services.evidence_report_artifacts import persist_artifact


_PERIODIC_TYPES = {
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
    "PERIODIC_INTERFERENCE_PATH_COMPARISON",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE",
}
_DTMF_TYPES = {"DTMF_ABNORMAL", "DTMF_SIP_DIAL_MATCH", "DTMF_SIP_DIAL_MISMATCH"}
_RTP_TYPES = {"HIGH_DELTA", "PACKET_LOSS", "BURST_LOSS", "ONE_WAY_RTP_MEDIA", "PAYLOAD_CHANGE"}


def _findings(db: Session, report: PreliminaryEvidenceReport) -> list[EvidenceFinding]:
    return list(db.scalars(select(EvidenceFinding).where(
        EvidenceFinding.scope_type == report.scope_type,
        EvidenceFinding.scope_id == report.scope_id,
        EvidenceFinding.last_seen_report_version == report.version,
    ).order_by(EvidenceFinding.representative_time.asc())))


def _run_artifacts(db: Session, run: AnalyzerRun | None, types: set[str]) -> list[Artifact]:
    if run is None:
        return []
    return list(db.scalars(select(Artifact).where(
        Artifact.analyzer_run_id == run.id,
        Artifact.type.in_(sorted(types)),
    ).order_by(Artifact.created_at.asc())))


def _meta_scope(artifact: Artifact) -> dict:
    meta = artifact.metadata_json or {}
    nested = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
    return {**nested, **{k: v for k, v in meta.items() if k != "scope"}}


def _load_json(storage, artifact: Artifact | None) -> dict:
    if artifact is None:
        return {}
    try:
        return json.loads(storage.get_bytes(artifact.object_key).decode("utf-8"))
    except Exception:
        return {}


def _human_metadata(
    kind: str,
    *,
    base: dict,
    finding: EvidenceFinding,
    measurement: dict,
    priority: int,
    visual_instance_id: str | None = None,
) -> dict:
    explanation = build_human_explanation(finding, kind, measurement=measurement)
    out = {
        **base,
        "renderer_family": "HUMAN",
        "renderer_version": HUMAN_RENDERER_VERSION,
        "presentation_profile": PRESENTATION_PROFILE,
        "presentation_priority": int(priority),
        "visual_kind": kind,
        "visual_instance_id": visual_instance_id,
        "measurement": measurement,
        "human_explanation": explanation,
    }
    annotation = dict(out.get("annotation_contract") or {})
    annotation["human_explanation_required"] = True
    annotation["human_explanation_contract"] = "human-visual-explanation-v1"
    out["annotation_contract"] = annotation
    out["annotation_complete"] = bool(
        out.get("annotation_complete")
        and explanation.get("what_to_look_at")
        and explanation.get("meaning")
        and explanation.get("evidence_boundary")
        and explanation.get("plain_language_summary")
        and str(explanation.get("diagnostic_authority") or "NONE").upper() == "NONE"
    )
    return out


def _audit_failure(db: Session, report: PreliminaryEvidenceReport, finding: EvidenceFinding, kind: str, exc: Exception) -> None:
    audit(
        db,
        case_id=report.case_id,
        event_type="HUMAN_EVIDENCE_VISUAL_FAILED",
        target_type="evidence_finding",
        target_id=finding.id,
        detail={
            "visual_kind": kind,
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:500],
            "fallback": "EXISTING_HUMAN_OR_MACHINE",
            "renderer_version": HUMAN_RENDERER_VERSION,
        },
    )


def _pcm_sessions(pcm: dict) -> dict[tuple[str, int], dict]:
    out: dict[tuple[str, int], dict] = {}
    for stream in pcm.get("streams", []) or []:
        tap = str((stream.get("tap") or {}).get("name") or "")
        direction = (stream.get("tap") or {}).get("direction")
        for session in stream.get("sessions", []) or []:
            out[(tap, int(session.get("session_index") or 0))] = {**session, "direction": direction}
    return out


def _pcm_wav_lookup(rows: list[Artifact]) -> dict[tuple[str, int], Artifact]:
    out = {}
    for row in rows:
        meta = row.metadata_json or {}
        tap = str(meta.get("pcm_tap") or "")
        if tap:
            out[(tap, int(meta.get("session_index") or 0))] = row
    return out


def _waveform_lookup(storage, rows: list[Artifact]) -> tuple[dict[tuple[str, int], tuple[Artifact, dict]], dict[str, tuple[Artifact, dict]]]:
    pcm: dict[tuple[str, int], tuple[Artifact, dict]] = {}
    rtp: dict[str, tuple[Artifact, dict]] = {}
    for row in rows:
        meta = row.metadata_json or {}
        try:
            payload = json.loads(storage.get_bytes(row.object_key).decode("utf-8"))
        except Exception:
            continue
        if meta.get("pcm_tap"):
            pcm[(str(meta["pcm_tap"]), int(meta.get("session_index") or 0))] = (row, payload)
        elif meta.get("stream_id"):
            rtp[str(meta["stream_id"])] = (row, payload)
    return pcm, rtp


def _dtmf_context(media: dict, tap: str, session_index: int) -> tuple[str | None, str | None, str | None]:
    for event in media.get("cross_layer_events", []) or []:
        if str(event.get("type") or "") not in {"DTMF_SIP_DIAL_MATCH", "DTMF_SIP_DIAL_MISMATCH"}:
            continue
        scope = event.get("scope") or {}
        details = event.get("details") or {}
        if str(scope.get("pcm_tap") or details.get("pcm_tap") or "") != tap:
            continue
        idx = scope.get("pcm_session_index", details.get("pcm_session_index"))
        if idx is not None and int(idx) != int(session_index):
            continue
        return details.get("pcm_digits"), details.get("sip_target"), str(event.get("type"))
    return None, None, None


def _select_dtmf_event(session: dict, finding: EvidenceFinding, pcm_sequence: str | None) -> dict | None:
    events = list(session.get("dtmf_events") or [])
    if not events:
        return None
    metrics = finding.metrics_json or {}
    event_index = metrics.get("event_index")
    if event_index is not None:
        try:
            index = int(event_index)
            if 0 <= index < len(events):
                return events[index]
        except (TypeError, ValueError):
            pass
    digit = str(metrics.get("digit") or "")
    if digit:
        matching = [e for e in events if str(e.get("digit") or "") == digit]
        if matching:
            events = matching
    base = session.get("start_time")
    if base is not None and finding.representative_time is not None:
        rel = float(finding.representative_time) - float(base)
        return min(events, key=lambda e: abs(float(e.get("start_seconds") or 0.0) - rel))
    if pcm_sequence:
        first = str(pcm_sequence)[0:1]
        matching = [e for e in events if str(e.get("digit") or "") == first]
        if matching:
            return matching[0]
    return events[0]


def _generate_dtmf(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    findings: list[EvidenceFinding],
    pcm: dict,
    media: dict,
    media_run: AnalyzerRun | None,
) -> list[Artifact]:
    if media_run is None:
        return []
    sessions = _pcm_sessions(pcm)
    wavs = _pcm_wav_lookup(_run_artifacts(db, media_run, {"PCM_WAV"}))
    created: list[Artifact] = []
    for finding in findings:
        if finding.finding_type not in _DTMF_TYPES:
            continue
        scope = finding.scope_json or {}
        tap = str(scope.get("pcm_tap") or "")
        idx = int(scope.get("pcm_session_index") or 0)
        session = sessions.get((tap, idx))
        wav = wavs.get((tap, idx))
        if not session or wav is None:
            continue
        pcm_sequence, sip_target, correlation_type = _dtmf_context(media, tap, idx)
        metrics = finding.metrics_json or {}
        pcm_sequence = str(metrics.get("pcm_digits") or pcm_sequence or "") or None
        sip_target = str(metrics.get("sip_target") or sip_target or "") or None
        event = _select_dtmf_event(session, finding, pcm_sequence)
        if event is None:
            continue
        try:
            raw = storage.get_bytes(wav.object_key)
            png, measurement = render_human_dtmf_inspector_png(
                raw,
                event,
                sip_target=sip_target,
                pcm_sequence=pcm_sequence,
                title=f"{finding.title} · DTMF Inspector",
            )
            measurement["correlation_event_type"] = correlation_type
            measurement["evidence_source_strategy"] = "PCM_WAV_ACCEPTED_DTMF_EVENT_WINDOW"
            base = visual_metadata(
                "DTMF_INSPECTOR",
                source={"source_artifact_id": wav.id, "pcm_tap": tap, "session_index": idx},
                window={"start": finding.start_time, "end": finding.end_time},
                title=f"{finding.title} · DTMF Inspector",
                finding_ids=[finding.id],
                call_id=scope.get("call_id") or report.call_id,
                direction=session.get("direction"),
                anomaly_window={"start": finding.start_time, "end": finding.end_time, "representative": finding.representative_time},
                caption="已接受 DTMF Event 的精细频域测量；未绑定版本化阈值的频偏/杂散指标仅展示测量值。",
            )
            created.append(persist_artifact(
                db, storage, report=report,
                artifact_type=EvidenceReportArtifactType.SPECTRUM_PNG.value,
                filename=f"00_human_finding_{finding.id[:8]}_{tap}_{idx}_dtmf_inspector.png",
                data=png, content_type="image/png",
                metadata=_human_metadata("DTMF_INSPECTOR", base=base, finding=finding, measurement=measurement, priority=320),
                analyzer_run_id=wav.analyzer_run_id, evidence_id=wav.evidence_id,
                finding_ids=[finding.id], role="FINDING",
            ))
        except Exception as exc:
            _audit_failure(db, report, finding, "DTMF_INSPECTOR", exc)
    return created


def _periodic_clip_and_metrics(db: Session, storage, media_run: AnalyzerRun | None, finding: EvidenceFinding) -> tuple[Artifact | None, dict]:
    if media_run is None:
        return None, {}
    scope = finding.scope_json or {}
    rows = _run_artifacts(db, media_run, {"PERIODIC_AUDIO_CLIP", "PERIODIC_METRICS_JSON"})
    clips = []
    metrics_rows = []
    for row in rows:
        meta = _meta_scope(row)
        if meta.get("pcm_tap") and scope.get("pcm_tap") and meta.get("pcm_tap") != scope.get("pcm_tap"):
            continue
        fidx = scope.get("pcm_session_index")
        ridx = meta.get("pcm_session_index", meta.get("session_index"))
        if fidx is not None and ridx is not None and int(fidx) != int(ridx):
            continue
        if row.type == "PERIODIC_AUDIO_CLIP" and str(meta.get("source") or "").lower() == "pcm_rx":
            clips.append(row)
        elif row.type == "PERIODIC_METRICS_JSON":
            metrics_rows.append(row)
    clip = clips[0] if clips else None
    metrics = _load_json(storage, metrics_rows[0]) if metrics_rows else {}
    return clip, metrics


def _generate_aligned_periodic_spectrum(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    findings: list[EvidenceFinding],
    media_run: AnalyzerRun | None,
) -> list[Artifact]:
    created: list[Artifact] = []
    for finding in findings:
        if finding.finding_type not in _PERIODIC_TYPES:
            continue
        clip, metrics_json = _periodic_clip_and_metrics(db, storage, media_run, finding)
        if clip is None:
            continue
        try:
            periodic = periodic_measurement(metrics_json or {"details": finding.metrics_json or {}}, source="pcm_rx")
            refs = periodic.get("harmonics_hz") or []
            raw = storage.get_bytes(clip.object_key)
            png, renderer_measurement = render_human_spectrum_png_from_wav(
                raw,
                canonical_spectral={},
                reference_frequencies_hz=refs,
                title=f"{finding.title} · 证据窗口连续频谱",
                subtitle="PCM RX · representative Evidence Window",
                max_frequency_hz=1200.0,
                max_seconds=2.0,
            )
            measurement = merge_visual_measurement(
                renderer_measurement,
                periodic,
                evidence_source_strategy="PERIODIC_AUDIO_CLIP",
                time_window_seconds=[0.0, periodic.get("representative_duration_seconds") or 1.0],
            )
            measurement["peak_marker_scope"] = "SAME_REPRESENTATIVE_EVIDENCE_WINDOW"
            scope = finding.scope_json or {}
            base = visual_metadata(
                "SPECTRUM",
                source={"source_artifact_id": clip.id, "source_artifact_type": clip.type, "peak_marker_scope": "SAME_REPRESENTATIVE_EVIDENCE_WINDOW"},
                window={"start": finding.start_time, "end": finding.end_time},
                title=f"{finding.title} · 证据窗口连续频谱",
                x_axis="Frequency", y_axis="Spectrum level",
                units={"x": "Hz", "y": "dBFS"},
                legend=["continuous FFT", "same-window periodic harmonic references"],
                finding_ids=[finding.id], call_id=scope.get("call_id") or report.call_id,
                direction=scope.get("direction") or scope.get("pcm_direction"),
                anomaly_window={"start": finding.start_time, "end": finding.end_time, "representative": finding.representative_time},
                caption="频谱曲线与谐波 Marker 均来自同一代表性 Evidence Window，不混用整段 Session Peak。",
            )
            created.append(persist_artifact(
                db, storage, report=report,
                artifact_type=EvidenceReportArtifactType.SPECTRUM_PNG.value,
                filename=f"00_human_finding_{finding.id[:8]}_periodic_spectrum_aligned.png",
                data=png, content_type="image/png",
                metadata=_human_metadata("SPECTRUM", base=base, finding=finding, measurement=measurement, priority=290),
                analyzer_run_id=clip.analyzer_run_id, evidence_id=clip.evidence_id,
                finding_ids=[finding.id], role="FINDING",
            ))
        except Exception as exc:
            _audit_failure(db, report, finding, "SPECTRUM_ALIGNED", exc)
    return created


def _track_start_maps(media: dict, pcm: dict) -> tuple[dict[tuple[str, int], float], dict[str, float]]:
    pcm_starts: dict[tuple[str, int], float] = {}
    for stream in pcm.get("streams", []) or []:
        tap = str((stream.get("tap") or {}).get("name") or "")
        for session in stream.get("sessions", []) or []:
            if session.get("start_time") is not None:
                pcm_starts[(tap, int(session.get("session_index") or 0))] = float(session["start_time"])
    for track in media.get("pcm_audio_tracks", []) or []:
        if track.get("start_time") is not None:
            pcm_starts[(str(track.get("pcm_tap") or ""), int(track.get("session_index") or 0))] = float(track["start_time"])
    rtp_starts = {
        str(track.get("stream_id")): float(track.get("start_time"))
        for track in media.get("rtp_audio_tracks", []) or []
        if track.get("stream_id") and track.get("start_time") is not None
    }
    return pcm_starts, rtp_starts


def _generate_multitrack(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    findings: list[EvidenceFinding],
    pcm: dict,
    media: dict,
    media_run: AnalyzerRun | None,
) -> list[Artifact]:
    if media_run is None:
        return []
    waveform_rows = _run_artifacts(db, media_run, {"WAVEFORM_JSON"})
    pcm_wave, rtp_wave = _waveform_lookup(storage, waveform_rows)
    pcm_starts, rtp_starts = _track_start_maps(media, pcm)
    created: list[Artifact] = []
    for finding in findings:
        if finding.finding_type not in _PERIODIC_TYPES:
            continue
        scope = finding.scope_json or {}
        if finding.start_time is None:
            continue
        window_start = float(finding.start_time)
        window_end = float(finding.end_time if finding.end_time is not None else window_start + 1.0)
        if window_end <= window_start:
            window_end = window_start + 1.0
        tap = str(scope.get("pcm_tap") or "pcm_rx")
        idx = int(scope.get("pcm_session_index") or 0)
        tracks: list[dict] = []
        source_ids: list[str] = []
        pcm_rx = pcm_wave.get((tap, idx))
        if pcm_rx:
            source_ids.append(pcm_rx[0].id)
            tracks.append({"label": "PCM RX" if tap == "pcm_rx" else tap.upper(), "start_time": pcm_starts.get((tap, idx), window_start), "waveform": pcm_rx[1]})
        up_id = scope.get("upstream_rtp_stream_id")
        if up_id and str(up_id) in rtp_wave:
            row, waveform = rtp_wave[str(up_id)]; source_ids.append(row.id)
            tracks.append({"label": "RTP Uplink", "start_time": rtp_starts.get(str(up_id), window_start), "waveform": waveform})
        down_id = scope.get("downstream_rtp_stream_id")
        if down_id and str(down_id) in rtp_wave:
            row, waveform = rtp_wave[str(down_id)]; source_ids.append(row.id)
            tracks.append({"label": "RTP Downlink", "start_time": rtp_starts.get(str(down_id), window_start), "waveform": waveform})
        pcm_tx = pcm_wave.get(("pcm_tx", idx))
        if pcm_tx:
            source_ids.append(pcm_tx[0].id)
            tracks.append({"label": "PCM TX", "start_time": pcm_starts.get(("pcm_tx", idx), window_start), "waveform": pcm_tx[1]})
        if len(tracks) < 2:
            continue
        try:
            png, measurement = render_human_multitrack_png(
                tracks,
                window_start=window_start,
                window_end=window_end,
                anomaly_start=window_start,
                anomaly_end=window_end,
                events=[{"time": finding.representative_time, "label": finding.finding_type}] if finding.representative_time is not None else [],
                title=f"{finding.title} · 跨层同轴波形",
            )
            measurement["source_artifact_ids"] = source_ids
            measurement["evidence_window_authority"] = "CANONICAL_FINDING_TIME_RANGE"
            base = visual_metadata(
                "MULTI_TRACK",
                source={"source_artifact_ids": source_ids},
                window={"start": window_start, "end": window_end},
                title=f"{finding.title} · 跨层同轴波形",
                finding_ids=[finding.id], call_id=scope.get("call_id") or report.call_id,
                anomaly_window={"start": window_start, "end": window_end, "representative": finding.representative_time},
                caption="可用 PCM/RTP 波形按同一 Canonical Evidence Window 对齐，仅用于跨层观察，不自行确认根因。",
            )
            created.append(persist_artifact(
                db, storage, report=report,
                artifact_type=EvidenceReportArtifactType.WAVEFORM_PNG.value,
                filename=f"00_human_finding_{finding.id[:8]}_multitrack.png",
                data=png, content_type="image/png",
                metadata=_human_metadata("MULTI_TRACK", base=base, finding=finding, measurement=measurement, priority=310),
                analyzer_run_id=media_run.id, finding_ids=[finding.id], role="FINDING",
            ))
        except Exception as exc:
            _audit_failure(db, report, finding, "MULTI_TRACK", exc)
    return created


def _periodic_layer(node: dict | None, label: str) -> dict:
    if not isinstance(node, dict):
        return {"name": label, "available": False, "status": "UNAVAILABLE"}
    rep = node.get("representative") or {}
    return {
        "name": label,
        "available": True,
        "status": str(node.get("level") or "AVAILABLE"),
        "rms_dbfs": rep.get("rms_dbfs"),
    }


def _generate_cross_layer(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    findings: list[EvidenceFinding],
    media_run: AnalyzerRun | None,
) -> list[Artifact]:
    if media_run is None:
        return []
    created: list[Artifact] = []
    for finding in findings:
        if finding.finding_type not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON"}:
            continue
        metrics = finding.metrics_json or {}
        correlation = metrics.get("correlation") or {}
        canonical = (finding.correlation_json or {}).get("first_observable_boundary") or {}
        boundary_statement = canonical.get("statement") if canonical.get("status") == "OBSERVED_BOUNDARY" else None
        layers = [
            _periodic_layer(metrics.get("downstream_rtp"), "RTP_DOWNSTREAM"),
            _periodic_layer(metrics.get("pcm_rx"), "PCM_RX"),
            _periodic_layer(metrics.get("upstream_rtp"), "RTP_UPSTREAM"),
        ]
        correlations = []
        if correlation:
            correlations.append({
                "from": "PCM_RX", "to": "RTP_UPSTREAM",
                "absolute_correlation": correlation.get("absolute_correlation"),
                "lag_ms": correlation.get("lag_ms"),
                "quality": correlation.get("quality"),
            })
        try:
            png, measurement = render_human_cross_layer_png(
                layers,
                correlations,
                canonical_boundary_statement=boundary_statement,
                title=f"{finding.title} · 跨层证据路径",
            )
            measurement["canonical_boundary_status"] = canonical.get("status") or "UNKNOWN"
            base = visual_metadata(
                "CROSS_LAYER",
                source={"analyzer_run_id": media_run.id},
                window={"start": finding.start_time, "end": finding.end_time},
                title=f"{finding.title} · 跨层证据路径",
                finding_ids=[finding.id], call_id=(finding.scope_json or {}).get("call_id") or report.call_id,
                anomaly_window={"start": finding.start_time, "end": finding.end_time, "representative": finding.representative_time},
                caption="仅投影已有跨层相关性、可用性与 Canonical first-observable boundary，不进行新的根因推断。",
            )
            created.append(persist_artifact(
                db, storage, report=report,
                artifact_type=EvidenceReportArtifactType.WAVEFORM_PNG.value,
                filename=f"01_human_finding_{finding.id[:8]}_cross_layer.png",
                data=png, content_type="image/png",
                metadata=_human_metadata("CROSS_LAYER", base=base, finding=finding, measurement=measurement, priority=90),
                analyzer_run_id=media_run.id, finding_ids=[finding.id], role="FINDING",
            ))
        except Exception as exc:
            _audit_failure(db, report, finding, "CROSS_LAYER", exc)
    return created


def _generate_rtp(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    findings: list[EvidenceFinding],
    packet: dict,
    packet_run: AnalyzerRun | None,
) -> list[Artifact]:
    streams = {str(s.get("stream_id")): s for s in packet.get("rtp_streams", []) or []}
    created: list[Artifact] = []
    for finding in findings:
        if finding.finding_type not in _RTP_TYPES:
            continue
        scope = finding.scope_json or {}
        stream_id = scope.get("rtp_stream_id")
        stream = streams.get(str(stream_id)) if stream_id else None
        if not stream:
            continue
        try:
            png, measurement = render_human_rtp_timeline_png(
                stream,
                finding_type=finding.finding_type,
                finding_metrics=finding.metrics_json or {},
                title=f"{finding.title} · Human RTP Timeline",
            )
            measurement["canonical_root_cause_boundary"] = finding.root_cause_boundary
            base = visual_metadata(
                "RTP_TIMELINE",
                source={"analyzer_run_id": packet_run.id if packet_run else None, "stream_id": stream_id},
                window={"start": stream.get("start_time"), "end": stream.get("end_time")},
                title=f"{finding.title} · Human RTP Timeline",
                x_axis="Relative time", y_axis="RTP stream / event",
                units={"x": "s", "delta": "ms"},
                legend=[finding.finding_type], finding_ids=[finding.id],
                call_id=scope.get("call_id") or report.call_id,
                direction=scope.get("direction"),
                anomaly_window={"start": finding.start_time, "end": finding.end_time, "representative": finding.representative_time},
                caption="RTP Canonical Event 的人类可读投影；HIGH_DELTA 与 Packet Loss 保持严格语义分离。",
            )
            created.append(persist_artifact(
                db, storage, report=report,
                artifact_type=EvidenceReportArtifactType.RTP_TIMELINE_PNG.value,
                filename=f"00_human_finding_{finding.id[:8]}_rtp_timeline.png",
                data=png, content_type="image/png",
                metadata=_human_metadata("RTP_TIMELINE", base=base, finding=finding, measurement=measurement, priority=330),
                analyzer_run_id=packet_run.id if packet_run else None,
                finding_ids=[finding.id], role="FINDING",
            ))
        except Exception as exc:
            _audit_failure(db, report, finding, "RTP_TIMELINE", exc)
    return created


def generate_extended_human_visual_artifacts(
    db: Session,
    storage,
    *,
    report: PreliminaryEvidenceReport,
    results: dict[str, dict | None],
    runs: dict[str, AnalyzerRun],
) -> list[Artifact]:
    """Generate H3/H4 Human Evidence projections without changing Analyzer authority.

    Every output is additive. Any unavailable source simply suppresses that visual;
    callers must catch the outer failure so canonical report generation remains safe.
    """
    if not human_renderer_enabled():
        return []
    media = results.get("media_intelligence") or {}
    packet = results.get("packet_intelligence") or media.get("packet") or {}
    pcm = results.get("pcm_intelligence") or media.get("pcm") or {}
    packet_run = runs.get("packet_intelligence") or runs.get("media_intelligence")
    media_run = runs.get("media_intelligence")
    findings = _findings(db, report)
    created: list[Artifact] = []
    created.extend(_generate_dtmf(db, storage, report=report, findings=findings, pcm=pcm, media=media, media_run=media_run))
    created.extend(_generate_multitrack(db, storage, report=report, findings=findings, pcm=pcm, media=media, media_run=media_run))
    created.extend(_generate_cross_layer(db, storage, report=report, findings=findings, media_run=media_run))
    created.extend(_generate_aligned_periodic_spectrum(db, storage, report=report, findings=findings, media_run=media_run))
    created.extend(_generate_rtp(db, storage, report=report, findings=findings, packet=packet, packet_run=packet_run))
    return created

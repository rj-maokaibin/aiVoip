from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.reports.evidence_brief import render_report_html
from app.reports.evidence_visuals import (
    render_rtp_timeline_png,
    render_spectrum_png,
    render_spectrogram_png,
    render_waveform_png,
    visual_metadata,
)
from app.reports.prd_spec_v1_alignment import finalize_report_contract
from app.reports.sip_flow_visual import render_sip_call_flow_png


OFFLINE_CANONICAL_CONTRACT = "offline-canonical-evidence-report-v2"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_file() else None


def _artifact_id(sha256: str, artifact_type: str) -> str:
    return f"OFFLINE-{artifact_type}-{sha256[:16]}"


def _normalized_file_artifact(
    path: Path,
    *,
    artifact_type: str,
    content_type: str,
    case_id: str,
    metadata: dict | None = None,
) -> dict:
    sha = _sha256_file(path)
    return {
        "artifact_id": _artifact_id(sha, artifact_type),
        "type": artifact_type,
        "filename": path.name,
        "content_type": content_type,
        "sha256": sha,
        "size_bytes": path.stat().st_size,
        "local_path": str(path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            **(metadata or {}),
            "case_id": case_id,
            "offline_materialized": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _write_generated_artifact(
    output_dir: Path,
    *,
    filename: str,
    data: bytes,
    artifact_type: str,
    case_id: str,
    metadata: dict,
) -> dict:
    path = output_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _normalized_file_artifact(
        path,
        artifact_type=artifact_type,
        content_type="image/png",
        case_id=case_id,
        metadata=metadata,
    )


def _load_json_artifact(artifact: dict) -> dict | None:
    path = _safe_path(artifact.get("local_path"))
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_analyzer_artifacts(media: dict, *, case_id: str) -> list[dict]:
    out = []
    for item in media.get("artifacts", []) or []:
        path = _safe_path(item.get("local_path"))
        if not path:
            continue
        out.append(_normalized_file_artifact(
            path,
            artifact_type=str(item.get("type") or "ANALYZER_ARTIFACT"),
            content_type=str(item.get("content_type") or "application/octet-stream"),
            case_id=case_id,
            metadata={
                **(item.get("metadata") or {}),
                "analyzer_name": "media_intelligence",
                "analyzer_version": media.get("version"),
                "profile_version": ((media.get("analyzer_profile") or {}).get("version") or "offline"),
                "source_artifact_ids": [],
            },
        ))
    return out


def _pcm_session(pcm: dict, tap_name: str) -> tuple[dict, dict] | tuple[None, None]:
    for stream in pcm.get("streams", []) or []:
        tap = stream.get("tap") or {}
        if str(tap.get("name") or "").lower() != tap_name.lower():
            continue
        sessions = stream.get("sessions") or []
        if sessions:
            return stream, sessions[0]
    return None, None


def _generate_summary_visuals(bundle: dict, output_dir: Path, *, case_id: str) -> list[dict]:
    packet = bundle.get("packet") or {}
    pcm = bundle.get("pcm") or {}
    media = bundle.get("media") or {}
    artifacts: list[dict] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    streams = packet.get("rtp_streams") or []
    if streams:
        metadata = visual_metadata(
            "RTP_TIMELINE",
            source={"analyzer_name": "packet_intelligence", "analyzer_version": packet.get("version")},
            title="RTP Timeline - Real Offline PCAP",
            x_axis="Time",
            y_axis="RTP Stream / Event",
            units={"x": "s"},
            caption="真实离线 PCAP 的 RTP Stream 与 Delta/Loss 事件时间线。",
        )
        artifacts.append(_write_generated_artifact(
            output_dir,
            filename="rtp_timeline_v2.png",
            data=render_rtp_timeline_png(streams, title="RTP TIMELINE", subtitle="REAL OFFLINE PCAP"),
            artifact_type="RTP_TIMELINE_PNG",
            case_id=case_id,
            metadata=metadata,
        ))

    calls = packet.get("calls") or []
    if calls:
        metadata = visual_metadata(
            "SIP_CALL_FLOW",
            source={"analyzer_name": "packet_intelligence", "analyzer_version": packet.get("version")},
            title="SIP Call Flow - Real Offline PCAP",
            x_axis="SIP endpoint",
            y_axis="Message order",
            units={"frame": "Frame"},
            caption="真实离线 PCAP 的 SIP Call Flow；用于复核 Call-ID、号码、关键 Frame 与建链流程。",
        )
        artifacts.append(_write_generated_artifact(
            output_dir,
            filename="sip_call_flow_v2.png",
            data=render_sip_call_flow_png(calls, title="SIP CALL FLOW", subtitle="REAL OFFLINE PCAP"),
            artifact_type="SIP_CALL_FLOW_PNG",
            case_id=case_id,
            metadata=metadata,
        ))

    pcm_stream, session = _pcm_session(pcm, "pcm_rx")
    if session:
        spectral = session.get("spectral") or {}
        if spectral:
            metadata = visual_metadata(
                "SPECTRUM",
                source={"pcm_tap": "pcm_rx", "session_index": session.get("session_index"), "direction": (pcm_stream.get("tap") or {}).get("direction")},
                window={"start": session.get("start_time"), "end": session.get("end_time")},
                title="PCM RX Periodic Interference Spectrum",
                x_axis="Frequency",
                y_axis="Magnitude / Energy ratio",
                units={"x": "Hz", "y": "dB or ratio"},
                legend=["50/60Hz family reference", "spectral peaks"],
                direction=(pcm_stream.get("tap") or {}).get("direction"),
                caption="PCM RX 周期干扰频谱；频率族特征用于证据边界，不直接确认电源/接地/SLIC 根因。",
            )
            artifacts.append(_write_generated_artifact(
                output_dir,
                filename="pcm_rx_periodic_spectrum_v2.png",
                data=render_spectrum_png(spectral, title="PCM RX PERIODIC SPECTRUM", subtitle="REAL OFFLINE PCAP"),
                artifact_type="SPECTRUM_PNG",
                case_id=case_id,
                metadata=metadata,
            ))

    raw = media.get("artifacts") or []
    for source_type, renderer, out_type, filename in (
        ("WAVEFORM_JSON", render_waveform_png, "WAVEFORM_PNG", "pcm_rx_waveform_v2.png"),
        ("SPECTROGRAM_JSON", render_spectrogram_png, "SPECTROGRAM_PNG", "pcm_rx_spectrogram_v2.png"),
    ):
        source = next((a for a in raw if a.get("type") == source_type and str((a.get("metadata") or {}).get("pcm_tap") or "").lower() == "pcm_rx"), None)
        data_json = _load_json_artifact(source or {})
        if not source or not data_json:
            continue
        meta = source.get("metadata") or {}
        if out_type == "WAVEFORM_PNG":
            png = renderer(data_json, title="PCM RX WAVEFORM", subtitle="REAL OFFLINE PCAP")
            x_axis, y_axis, units = "Time", "Amplitude", {"x": "s", "y": "PCM"}
        else:
            png = renderer(data_json, title="PCM RX SPECTROGRAM", subtitle="REAL OFFLINE PCAP")
            x_axis, y_axis, units = "Time", "Frequency", {"x": "s", "y": "Hz"}
        metadata = visual_metadata(
            out_type.replace("_PNG", ""),
            source={"source_artifact_id": source.get("filename"), "pcm_tap": "pcm_rx", "session_index": meta.get("session_index")},
            title=filename.replace("_", " ").upper(),
            x_axis=x_axis,
            y_axis=y_axis,
            units=units,
            caption=f"PCM RX {out_type.replace('_PNG', '').title()}；用于周期干扰 Finding 直接下钻。",
        )
        artifacts.append(_write_generated_artifact(
            output_dir,
            filename=filename,
            data=png,
            artifact_type=out_type,
            case_id=case_id,
            metadata=metadata,
        ))
    return artifacts


def _artifact_ref(item: dict, role: str = "FINDING") -> dict:
    return {
        "artifact_id": item.get("artifact_id"),
        "type": item.get("type"),
        "filename": item.get("filename"),
        "content_type": item.get("content_type"),
        "sha256": item.get("sha256"),
        "size_bytes": item.get("size_bytes"),
        "local_path": item.get("local_path"),
        "role": role,
        "metadata": item.get("metadata") or {},
    }


def _attach_offline_refs(report: dict, artifacts: list[dict]) -> None:
    periodic_clips = [a for a in artifacts if a.get("type") == "PERIODIC_AUDIO_CLIP"]
    pcm_visuals = [a for a in artifacts if a.get("type") in {"SPECTRUM_PNG", "SPECTROGRAM_PNG", "WAVEFORM_PNG"} and "pcm_rx" in str(a.get("filename") or "").lower()]
    rtp_timeline = [a for a in artifacts if a.get("type") == "RTP_TIMELINE_PNG"]
    sip_flow = [a for a in artifacts if a.get("type") == "SIP_CALL_FLOW_PNG"]
    event_audio = [a for a in artifacts if a.get("type") == "AUDIO_CLIP"]

    for finding in report.get("findings") or []:
        ftype = str(finding.get("type") or "")
        refs: list[dict] = []
        if ftype in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON"}:
            refs.extend(_artifact_ref(a) for a in pcm_visuals)
            refs.extend(_artifact_ref(a) for a in periodic_clips)
        elif ftype in {"HIGH_DELTA", "PACKET_LOSS", "BURST_LOSS", "ONE_WAY_RTP_MEDIA", "PAYLOAD_CHANGE"}:
            refs.extend(_artifact_ref(a) for a in rtp_timeline)
            scope = finding.get("scope") or {}
            stream_id = scope.get("rtp_stream_id")
            for item in event_audio:
                meta = item.get("metadata") or {}
                if stream_id and meta.get("stream_id") not in (None, stream_id):
                    continue
                event_type = str(meta.get("event_type") or "")
                if event_type and event_type != ftype:
                    continue
                refs.append(_artifact_ref(item))
        elif ftype.startswith("SIP_") or ftype == "CODEC_NEGOTIATION_MISMATCH":
            refs.extend(_artifact_ref(a) for a in sip_flow)
        else:
            scope = finding.get("scope") or {}
            if str(scope.get("pcm_tap") or "").lower() == "pcm_rx":
                refs.extend(_artifact_ref(a) for a in pcm_visuals)
        seen = set()
        finding["artifact_refs"] = [ref for ref in refs if not (ref.get("artifact_id") in seen or seen.add(ref.get("artifact_id")))]


def _build_bundle(
    *,
    report: dict,
    source_pcap: Path,
    artifacts: list[dict],
    output_dir: Path,
) -> dict:
    bundle_dir = output_dir / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    report_json = bundle_dir / "preliminary-evidence-report-v2.json"
    report_html = bundle_dir / "preliminary-evidence-report-v2.html"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_html.write_text(render_report_html(report), encoding="utf-8")

    files: list[tuple[str, Path, str]] = [
        (f"pcap/{source_pcap.name}", source_pcap, "RAW_PCAP"),
        ("report/preliminary-evidence-report-v2.json", report_json, "PRELIMINARY_REPORT_JSON"),
        ("report/preliminary-evidence-report-v2.html", report_html, "PRELIMINARY_REPORT_HTML"),
    ]
    for item in artifacts:
        path = _safe_path(item.get("local_path"))
        if not path:
            continue
        atype = str(item.get("type") or "ARTIFACT")
        folder = "audio" if str(item.get("content_type") or "").startswith("audio/") else "images" if str(item.get("content_type") or "").startswith("image/") else "analyzer"
        files.append((f"{folder}/{path.name}", path, atype))

    unique: dict[str, tuple[Path, str]] = {}
    for archive_path, source, atype in files:
        unique.setdefault(archive_path, (source, atype))

    manifest_files = []
    sums = []
    for archive_path, (source, atype) in sorted(unique.items()):
        sha = _sha256_file(source)
        size = source.stat().st_size
        manifest_files.append({"path": archive_path, "sha256": sha, "size_bytes": size, "type": atype})
        sums.append((sha, archive_path))
    manifest = {
        "schema_version": "evidence-bundle-v1",
        "profile": "INTERNAL_FULL",
        "report_version": report.get("version") or report.get("report_version"),
        "source_pcap_sha256": _sha256_file(source_pcap),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest_files,
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sums.append((_sha256_file(manifest_path), "manifest.json"))
    sums_path = bundle_dir / "SHA256SUMS"
    sums_path.write_text("\n".join(f"{sha}  {path}" for sha, path in sorted(sums)) + "\n", encoding="utf-8")

    zip_path = output_dir / "evidence-bundle-internal-full-v2.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for archive_path, (source, _) in sorted(unique.items()):
            zf.write(source, archive_path)
        zf.write(manifest_path, "manifest.json")
        zf.write(sums_path, "SHA256SUMS")
    return {
        "schema_version": "evidence-bundle-v1",
        "profile": "INTERNAL_FULL",
        "filename": zip_path.name,
        "local_path": str(zip_path),
        "sha256": _sha256_file(zip_path),
        "size_bytes": zip_path.stat().st_size,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "file_count": len(unique) + 2,
    }


def finalize_offline_analysis_bundle_v2(
    bundle: dict,
    *,
    source_pcap: str | Path,
    output_dir: str | Path,
) -> dict:
    """Materialize an Offline Golden replay into the same Canonical V2 contract.

    This adapter never invents missing event timing or a physical root cause. It
    only promotes Analyzer-produced local files into deterministic report-safe
    artifacts and then invokes the shared canonical finalizer used by runtime
    reports.
    """
    source_pcap = Path(source_pcap)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = copy.deepcopy(bundle.get("report") or {})
    diagnosis = copy.deepcopy(bundle.get("diagnosis") or {})
    report["diagnosis"] = diagnosis
    report["report_version"] = 2
    report["version"] = 2
    report["offline_canonical_contract"] = OFFLINE_CANONICAL_CONTRACT

    case = report.get("case") or {}
    case_id = str(case.get("id") or "offline-case")
    artifacts = _normalize_analyzer_artifacts(bundle.get("media") or {}, case_id=case_id)
    artifacts.extend(_generate_summary_visuals(bundle, output_dir / "visuals", case_id=case_id))
    raw_pcap_artifact = _normalized_file_artifact(
        source_pcap,
        artifact_type="RAW_PCAP",
        content_type="application/vnd.tcpdump.pcap",
        case_id=case_id,
        metadata={
            "source": "USER_UPLOAD",
            "analyzer_name": "source-evidence",
            "analyzer_version": "raw",
            "profile_version": "n/a",
        },
    )
    artifacts.insert(0, raw_pcap_artifact)
    report["artifacts"] = artifacts
    _attach_offline_refs(report, artifacts)

    display_call = report.get("display_call") or report.get("call") or {}
    pseudo = SimpleNamespace(
        id="OFFLINE-CANONICAL-V2",
        case_id=case_id,
        session_id=None,
        call_id=display_call.get("id") or display_call.get("call_id"),
        scope_type=(report.get("scope") or {}).get("type") or "CASE",
        scope_id=(report.get("scope") or {}).get("id") or case_id,
        version=2,
        status="COMPOSING",
    )
    finalize_report_contract(pseudo, report)
    report["version"] = 2
    report["report_version"] = 2
    report["status"] = pseudo.status
    report["evidence_bundle_summary"] = {
        "status": "TO_BE_MATERIALIZED",
        "profile": "INTERNAL_FULL",
        "manifest_schema": "evidence-bundle-v1",
        "contains": ["report", "raw_pcap", "audio", "images", "analyzer_json", "manifest", "SHA256SUMS"],
    }

    bundle_summary = _build_bundle(report=report, source_pcap=source_pcap, artifacts=artifacts, output_dir=output_dir)
    report["evidence_bundle_summary"] = {"status": "AVAILABLE", **bundle_summary}
    # Bundle metadata changes only the projection convenience section; it cannot
    # change Finding truth. Re-run finalization so Feishu/Web share this summary.
    finalize_report_contract(pseudo, report)
    report["version"] = 2
    report["report_version"] = 2
    report["status"] = pseudo.status

    final_json = output_dir / "canonical-report-v2.json"
    final_html = output_dir / "canonical-report-v2.html"
    final_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    final_html.write_text(render_report_html(report), encoding="utf-8")
    return {
        **bundle,
        "schema_version": "offline-analysis-replay-bundle-v2",
        "report": report,
        "canonical_report_json": str(final_json),
        "canonical_report_html": str(final_html),
        "evidence_bundle": bundle_summary,
    }

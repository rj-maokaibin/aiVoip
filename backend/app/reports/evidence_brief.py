from __future__ import annotations

from .evidence_brief_core import *  # noqa: F401,F403
from .evidence_brief_core import render_report_html as _core_render_report_html
from .evidence_package import build_report_evidence_packages


_SUMMARY_GRAPH_BY_FINDING = {
    "SIP_REGISTRATION_FAILED": "SIP_CALL_FLOW_PNG",
    "SIP_CALL_FAILED": "SIP_CALL_FLOW_PNG",
    "SIP_CONFLICTING_FINAL_RESPONSE": "SIP_CALL_FLOW_PNG",
    "CODEC_NEGOTIATION_MISMATCH": "SIP_CALL_FLOW_PNG",
    "PACKET_LOSS": "RTP_TIMELINE_PNG",
    "BURST_LOSS": "RTP_TIMELINE_PNG",
    "HIGH_DELTA": "RTP_TIMELINE_PNG",
    "PAYLOAD_CHANGE": "RTP_TIMELINE_PNG",
    "ONE_WAY_RTP_MEDIA": "RTP_TIMELINE_PNG",
}


def _attach_deterministic_summary_graphs(payload: dict) -> None:
    """Map report-wide SIP/RTP summary visuals to compatible Findings.

    The renderer creates one deterministic Call Flow / RTP Timeline per report.
    Those artifacts may have report-level links rather than finding-level links;
    this projection makes the relationship explicit in Canonical Report JSON.
    """
    artifacts = payload.get("artifacts") or []
    by_type = {}
    for artifact in artifacts:
        by_type.setdefault(str(artifact.get("type") or "").upper(), []).append(artifact)
    for finding in payload.get("findings", []) or []:
        required = _SUMMARY_GRAPH_BY_FINDING.get(str(finding.get("type") or ""))
        if not required:
            continue
        refs = list(finding.get("artifact_refs") or [])
        if any(str(x.get("type") or "").upper() == required for x in refs):
            continue
        candidates = by_type.get(required) or []
        if not candidates:
            continue
        artifact = candidates[0]
        refs.append({
            "artifact_id": artifact.get("artifact_id"),
            "type": artifact.get("type"),
            "filename": artifact.get("filename"),
            "content_type": artifact.get("content_type"),
            "sha256": artifact.get("sha256"),
            "metadata": artifact.get("metadata") or {},
            "role": "SUMMARY_GRAPH",
            "mapping_reason": "DETERMINISTIC_FINDING_TYPE_TO_REPORT_SUMMARY_VISUAL",
        })
        finding["artifact_refs"] = refs


def render_report_html(payload: dict) -> str:
    _attach_deterministic_summary_graphs(payload)
    packages = build_report_evidence_packages(payload.get("findings") or [])
    payload["finding_evidence_packages"] = packages
    payload["reviewability"] = packages.get("summary") or {}
    return _core_render_report_html(payload)

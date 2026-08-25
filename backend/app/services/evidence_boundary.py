from __future__ import annotations

from app.reports.diagnostic_contract import (
    attach_finding_diagnostic_links,
    build_diagnostic_contract_snapshot,
    validate_diagnostic_contract_snapshot,
)
from app.reports.finding_composer import derive_first_observable_layer


_DIAGNOSTIC_SNAPSHOT_KEY = "__diagnostic_contract_snapshot"


def _periodic_abnormal(node: dict | None) -> tuple[bool,bool]:
    if node is None:
        return False, False
    level=str(node.get("level") or "LOW").upper()
    return True, level in {"MEDIUM","HIGH"}


def _take_diagnostic_snapshot(payload: dict) -> dict:
    """Consume the private Analyzer→Composer transport before publication."""
    analyzers=payload.get("analyzers") or {}
    packet_state=analyzers.get("packet_intelligence") if isinstance(analyzers,dict) else None
    if isinstance(packet_state,dict):
        snapshot=packet_state.pop(_DIAGNOSTIC_SNAPSHOT_KEY,None)
        if isinstance(snapshot,dict):
            return snapshot

    # Compatibility with early PR7 stacked commits / direct fixtures.
    media_summary=payload.get("media_summary")
    if isinstance(media_summary,dict):
        snapshot=media_summary.pop(_DIAGNOSTIC_SNAPSHOT_KEY,None)
        if isinstance(snapshot,dict):
            return snapshot
    pcm_summary=payload.get("pcm_summary") or {}
    embedded=pcm_summary.get("summary") if isinstance(pcm_summary,dict) else None
    if isinstance(embedded,dict):
        snapshot=embedded.pop(_DIAGNOSTIC_SNAPSHOT_KEY,None)
        if isinstance(snapshot,dict):
            return snapshot

    # Direct unit callers of build_report_payload do not pass through
    # load_analyzer_results. Start from an empty valid snapshot; the explicit
    # Finding adapter below records every compatibility fallback.
    return build_diagnostic_contract_snapshot(results={},analyzer_states={})


def _append_diagnostic_event_refs(finding: dict, link: dict, snapshot: dict) -> None:
    """Bridge PR7 canonical IDs into the existing Claim/DB event-ref channel."""
    refs=list(finding.get("event_refs") or [])
    decision_by_id={str(x.get("decision_id")):x for x in snapshot.get("candidate_decisions",[]) or [] if x.get("decision_id")}
    canonical=[]
    for event_id in link.get("event_ids") or []:
        canonical.append({"source":"diagnostic.events","event_id":str(event_id)})
    for decision_id in link.get("decision_ids") or []:
        decision=decision_by_id.get(str(decision_id)) or {}
        canonical.append({
            "source":"diagnostic.decisions",
            "decision_id":str(decision_id),
            "status":decision.get("status"),
            "reason_code":decision.get("reason_code"),
        })
    for item in canonical:
        if item not in refs:
            refs.append(item)
    finding["event_refs"]=refs


def _attach_diagnostic_contract(payload: dict) -> None:
    findings=payload.get("findings",[]) or []
    snapshot=_take_diagnostic_snapshot(payload)
    attach_finding_diagnostic_links(findings=findings,snapshot=snapshot)
    validate_diagnostic_contract_snapshot(snapshot,findings=findings)
    payload["diagnostic_contract"]=snapshot

    # EvidenceFinding currently has no dedicated DB JSON column for the PR7
    # contract. Persist stable references in existing correlation_json/event_refs;
    # complete immutable Event/Decision objects remain authoritative in snapshot_json.
    for finding in findings:
        link=finding.get("diagnostic") or {}
        correlation=dict(finding.get("correlation") or {})
        correlation["diagnostic_contract"]={
            "schema_version":link.get("schema_version"),
            "event_ids":link.get("event_ids") or [],
            "decision_ids":link.get("decision_ids") or [],
            "accepted_event_ids":link.get("accepted_event_ids") or [],
            "merged_event_ids":link.get("merged_event_ids") or [],
        }
        finding["correlation"]=correlation
        _append_diagnostic_event_refs(finding,link,snapshot)


def apply_first_observable_boundaries(payload:dict) -> dict:
    """Enrich Findings without creating new root-cause authority.

    PR7 also finalizes the canonical DiagnosticEvent/CandidateDecision/Finding
    linkage here because this stage runs after Finding composition and before
    persistence, Artifact binding, Grounding and report publication.
    """
    for finding in payload.get("findings",[]) or []:
        if finding.get("type") not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_INTERFERENCE_PATH_COMPARISON"}:
            continue
        metrics=finding.get("metrics") or {}
        down_available,down_abnormal=_periodic_abnormal(metrics.get("downstream_rtp"))
        pcm_available,pcm_abnormal=_periodic_abnormal(metrics.get("pcm_rx"))
        up_available,up_abnormal=_periodic_abnormal(metrics.get("upstream_rtp"))
        result=derive_first_observable_layer([
            {"layer":"RTP_DOWNSTREAM","available":down_available,"abnormal":down_abnormal},
            {"layer":"PCM_RX","available":pcm_available,"abnormal":pcm_abnormal},
            {"layer":"RTP_UPSTREAM","available":up_available,"abnormal":up_abnormal},
        ])
        correlation=dict(finding.get("correlation") or {})
        correlation["first_observable_boundary"]=result
        correlation["role_boundary"]=(
            "RTP Downstream / Upstream 角色来自当前 Media Correlation 的方向映射；"
            "本字段仅描述当前采集链路中的首次可观测位置，不声明物理信号起源。"
        )
        finding["correlation"]=correlation
        if result.get("status")=="OBSERVED_BOUNDARY":
            finding["interpretation"]=(finding.get("interpretation") or "")+" "+result.get("statement","")
        elif result.get("status")=="UNKNOWN":
            finding["interpretation"]=(finding.get("interpretation") or "")+" 当前上游/对照层证据不完整，首次异常层保持 UNKNOWN（未知）。"
    _attach_diagnostic_contract(payload)
    return payload

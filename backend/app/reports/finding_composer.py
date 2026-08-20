from __future__ import annotations

from app.analyzers.candidate_gate import CandidateDecision, build_diagnostic_candidates, candidate_summary
from .finding_composer_core import *  # noqa: F401,F403
from .finding_composer_core import (
    _base_finding,
    _media_findings,
    _merge_same_signature,
    _packet_findings,
    _pcm_findings,
    build_normal_evidence as _core_build_normal_evidence,
    sort_findings,
)


_CONTEXT_GATED_AUDIO_TYPES = {"CLICK_POP", "UNEXPECTED_SILENCE"}


def _resolved_candidates(*, pcm: dict | None, media: dict | None) -> list[dict]:
    existing = list((media or {}).get("diagnostic_candidates", []) or [])
    return existing if existing else build_diagnostic_candidates(pcm=pcm, media=media)


def _candidate_findings(candidates: list[dict], source_run_id: str | None) -> list[dict]:
    out: list[dict] = []
    title_map = {
        "CLICK_POP": "活跃媒体窗口 Click/Pop（点击声/爆音）",
        "UNEXPECTED_SILENCE": "活跃媒体窗口异常静音",
    }
    for candidate in candidates:
        if candidate.get("decision") != CandidateDecision.ACCEPT.value:
            continue
        ftype = str(candidate.get("type") or "AUDIO_CANDIDATE")
        if ftype not in _CONTEXT_GATED_AUDIO_TYPES:
            continue
        scope = dict(candidate.get("scope") or {})
        scope.setdefault("layer", scope.get("pcm_tap") or "CROSS_LAYER")
        metrics = dict(candidate.get("metrics") or {})
        metrics.update({
            "candidate_id": candidate.get("candidate_id"),
            "candidate_decision": candidate.get("decision"),
            "candidate_reason_codes": list(candidate.get("reason_codes") or []),
            "candidate_context": dict(candidate.get("context") or {}),
        })
        observation = (
            f"{scope.get('pcm_tap') or 'PCM'} 的 {ftype} Detector Candidate 已通过 Call 级上下文与 Negative Control Gate。"
        )
        out.append(_base_finding(
            finding_type=ftype,
            severity=candidate.get("severity") or "MEDIUM",
            evidence_level=candidate.get("evidence_level") or "L3",
            title=title_map.get(ftype, f"音频异常：{ftype}"),
            observation=observation,
            interpretation=(
                "该事件已通过当前 V1 确定性上下文 Gate，可作为初步证据 Finding；"
                "仍需结合用户感知和更高等级证据确认最终故障结论。"
            ),
            scope=scope,
            metrics=metrics,
            time_range=dict(candidate.get("time_range") or {}),
            source_run_id=source_run_id,
            feature_family=ftype,
            correlation={"candidate_gate": {
                "decision": candidate.get("decision"),
                "reason_codes": list(candidate.get("reason_codes") or []),
                "context": dict(candidate.get("context") or {}),
            }},
            evidence_refs=[{"type": "ANALYZER_RUN", "id": source_run_id}] if source_run_id else [],
            event_refs=[dict(candidate.get("source_event_ref") or {})] if candidate.get("source_event_ref") else [],
        ))
    return out


def compose_findings(*, packet: dict | None = None, pcm: dict | None = None,
                     media: dict | None = None, source_run_ids: dict[str, str] | None = None) -> list[dict]:
    """Compose Findings after deterministic Candidate gates.

    Raw PCM CLICK_POP / UNEXPECTED_SILENCE are detector observations only and
    must never bypass the Media/Context Candidate gate. Other PCM findings keep
    their existing deterministic path.
    """
    source_run_ids = source_run_ids or {}
    findings: list[dict] = []
    findings.extend(_packet_findings(packet, source_run_ids.get("packet_intelligence")))
    findings.extend(
        f for f in _pcm_findings(pcm, source_run_ids.get("pcm_intelligence"))
        if f.get("type") not in _CONTEXT_GATED_AUDIO_TYPES
    )
    findings.extend(
        f for f in _media_findings(media, source_run_ids.get("media_intelligence"))
        if f.get("type") not in _CONTEXT_GATED_AUDIO_TYPES
    )
    candidates = _resolved_candidates(pcm=pcm, media=media)
    findings.extend(_candidate_findings(candidates, source_run_ids.get("media_intelligence")))
    return sort_findings(_merge_same_signature(findings))


def build_normal_evidence(packet: dict | None, pcm: dict | None, media: dict | None) -> list[dict]:
    normal = _core_build_normal_evidence(packet, pcm, media)
    candidates = _resolved_candidates(pcm=pcm, media=media)
    if not candidates:
        return normal
    summary = candidate_summary(candidates)
    suppressed = [x for x in candidates if x.get("decision") == CandidateDecision.SUPPRESS.value]
    inconclusive = [x for x in candidates if x.get("decision") == CandidateDecision.INCONCLUSIVE.value]
    if suppressed:
        reasons: dict[str, int] = {}
        for candidate in suppressed:
            for code in candidate.get("reason_codes") or []:
                if code == "ACTIVE_MEDIA_SCOPED":
                    continue
                reasons[code] = reasons.get(code, 0) + 1
        normal.append({
            "type": "AUDIO_CANDIDATES_SUPPRESSED",
            "text": f"{len(suppressed)} 个音频异常 Detector Candidate 因正常业务/跨层对照证据被抑制，不升级为 Finding。",
            "candidate_ids": [x.get("candidate_id") for x in suppressed],
            "reason_counts": reasons,
        })
    if inconclusive:
        normal.append({
            "type": "AUDIO_CANDIDATES_INCONCLUSIVE",
            "text": f"{len(inconclusive)} 个音频异常 Candidate 因跨层证据不足保持 INCONCLUSIVE，不升级为 Finding。",
            "candidate_ids": [x.get("candidate_id") for x in inconclusive],
        })
    normal.append({"type": "DIAGNOSTIC_CANDIDATE_SUMMARY", "text": f"音频 Candidate Gate：总计 {summary['total']}，ACCEPT {summary['decisions'].get('ACCEPT',0)}，SUPPRESS {summary['decisions'].get('SUPPRESS',0)}，INCONCLUSIVE {summary['decisions'].get('INCONCLUSIVE',0)}。", "summary": summary})
    return normal

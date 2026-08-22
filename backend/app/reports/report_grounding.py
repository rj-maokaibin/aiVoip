from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any


GROUNDING_VALIDATOR_VERSION = "report-grounding-v1"
CLAIM_MANIFEST_VERSION = "report-claim-manifest-v1"

BLOCKER = "BLOCKER"
WARNING = "WARNING"
PASS = "PASS"
PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
FAIL = "FAIL"

_AUDIO_EXPECTED_FINDINGS = {
    "PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PCM_GAP", "UNEXPECTED_SILENCE", "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED", "DTMF_ABNORMAL",
}
_VISUAL_REQUIRED_FINDINGS = {
    "PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA", "PCM_GAP", "UNEXPECTED_SILENCE", "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "ECHO_PATH_DETECTED",
}
_PACKET_REF_REQUIRED_FINDINGS = {"PACKET_LOSS", "BURST_LOSS", "HIGH_DELTA"}
_PERIODIC_FINDINGS = {
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE", "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON",
}
_REPORT_SAFE_AUDIO_TYPES = {"AUDIO_CLIP", "PERIODIC_AUDIO_CLIP"}
_REPORT_SAFE_IMAGE_TYPES = {"WAVEFORM_PNG", "SPECTRUM_PNG", "SPECTROGRAM_PNG", "RTP_TIMELINE_PNG", "SIP_CALL_FLOW_PNG"}


@dataclass(frozen=True, slots=True)
class GroundingIssue:
    rule_id: str
    layer: str
    severity: str
    code: str
    message: str
    finding_id: str | None = None
    finding_type: str | None = None
    actual: Any = None
    expected: Any = None


class ReportGroundingError(RuntimeError):
    def __init__(self, validation: dict):
        self.validation = validation
        blockers = [x for x in validation.get("issues", []) if x.get("severity") == BLOCKER]
        summary = "; ".join(f"{x.get('rule_id')}:{x.get('code')}" for x in blockers[:6]) or "UNKNOWN"
        super().__init__(f"REPORT_GROUNDING_FAILED:{summary}")


def _stable_claim_id(finding: dict) -> str:
    stable = str(finding.get("stable_key") or finding.get("finding_id") or finding.get("finding_signature") or finding.get("type") or "finding")
    return "CLAIM-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16].upper()


def _primitive_metrics(finding: dict) -> dict:
    metrics = finding.get("metrics") or {}
    out: dict[str, Any] = {}
    preferred = (
        "event_count", "max_delta_ms", "expected_ptime_ms", "ptime_ms", "stream_lost_packets", "lost_packets",
        "all_sequence_continuous", "max_excess_delay_ms", "duration_ms", "threshold_dbfs", "delay_ms",
        "absolute_correlation", "pcm_digits", "sip_target",
    )
    for key in preferred:
        value = metrics.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None and key in metrics:
            out[key] = value
    if not out:
        for key, value in metrics.items():
            if isinstance(value, (str, int, float, bool)):
                out[key] = value
            if len(out) >= 8:
                break
    return out


def build_claim_manifest(payload: dict) -> dict:
    """Project existing Finding truth into deterministic, traceable report claims."""
    claims: list[dict] = []
    for finding in payload.get("findings") or []:
        card = finding.get("evidence_card") or {}
        artifact_ids = [str(x.get("artifact_id")) for x in (finding.get("artifact_refs") or []) if x.get("artifact_id")]
        claims.append({
            "claim_id": _stable_claim_id(finding),
            "claim_type": finding.get("type"),
            "statement": finding.get("observation"),
            "finding_ref": finding.get("finding_id") or finding.get("stable_key"),
            "scope": finding.get("scope") or {},
            "metrics": _primitive_metrics(finding),
            "event_refs": finding.get("event_refs") or [],
            "packet_refs": card.get("packet_refs") or [],
            "artifact_refs": artifact_ids,
            "rule_id": f"CLAIM-{str(finding.get('type') or 'FINDING')}-V1",
        })
    return {"schema_version": CLAIM_MANIFEST_VERSION, "claim_count": len(claims), "claims": claims}


def _finding_key(finding: dict) -> tuple[str | None, str | None]:
    return finding.get("finding_id") or finding.get("stable_key"), finding.get("type")


def _is_publication_finding(finding: dict) -> bool:
    """Persisted report Findings have a database finding_id.

    In-memory Offline Golden replay builds Findings before report persistence and
    therefore cannot yet possess report-level PNG inventory or persisted IDs. It is
    still subject to structural Call and semantic rules, but final publication-only
    Artifact/Card completeness is enforced only once the Finding is persisted.
    """
    return bool(finding.get("finding_id"))


def _issue(issues: list[GroundingIssue], *, rule_id: str, layer: str, severity: str, code: str,
           message: str, finding: dict | None = None, actual: Any = None, expected: Any = None) -> None:
    finding_id, finding_type = _finding_key(finding or {})
    issues.append(GroundingIssue(rule_id, layer, severity, code, message, finding_id, finding_type, actual, expected))


def _packet_calls(payload: dict) -> int:
    try:
        return int((payload.get("packet_summary") or {}).get("call_count") or 0)
    except (TypeError, ValueError):
        return 0


def _artifact_index(payload: dict) -> dict[str, dict]:
    return {str(x.get("artifact_id")): x for x in (payload.get("artifacts") or []) if x.get("artifact_id")}


def _text_has_confirmed_physical_root_cause(finding: dict) -> bool:
    text = " ".join(str(finding.get(k) or "") for k in ("title", "observation", "interpretation")).lower()
    # Only explicit positive confirmation phrases are blocked. Negated boundaries
    # such as "不能确认电源/接地根因" must never be treated as overclaiming.
    forbidden = (
        "电源根因已确认", "已确认电源问题为根因", "根因确定为电源", "电源是根因",
        "接地根因已确认", "已确认接地问题为根因", "根因确定为接地", "接地是根因",
        "slic根因已确认", "slic 根因已确认", "根因确定为slic", "根因确定为 slic",
        "话柄根因已确认", "根因确定为话柄", "线路根因已确认", "根因确定为线路",
        "power supply confirmed", "power supply is the root cause", "grounding confirmed", "grounding is the root cause",
        "slic confirmed", "slic is the root cause",
    )
    return any(token in text for token in forbidden)


def _validate_structural(payload: dict, issues: list[GroundingIssue]) -> None:
    context = payload.get("analysis_context") or {}
    display_call = payload.get("display_call") or payload.get("call")
    reconstructed = int(context.get("reconstructed_call_count") or 0)
    call_scope = str(context.get("call_scope") or "")
    selection = str(context.get("call_selection_status") or "")
    if _packet_calls(payload) > 0 and reconstructed > 0 and call_scope == "BOUND" and selection in {"", "SELECTED"} and not display_call:
        _issue(issues, rule_id="RG-001", layer="STRUCTURAL", severity=BLOCKER, code="CALL_BINDING_CONTRADICTION",
               message="Packet/AnalysisContext 已重建并绑定 Call，但 Canonical Report 没有 display_call。",
               actual={"packet_call_count": _packet_calls(payload), "reconstructed_call_count": reconstructed, "display_call": display_call}, expected="bound display_call")

    if str(context.get("analysis_mode") or "") == "OFFLINE_IMPORTED" and payload.get("session"):
        _issue(issues, rule_id="RG-002", layer="STRUCTURAL", severity=BLOCKER, code="OFFLINE_SESSION_CONTRADICTION",
               message="Offline Imported 报告不得伪绑定 Reproduction Session。", actual=payload.get("session"), expected=None)

    artifacts = _artifact_index(payload)
    for finding in payload.get("findings") or []:
        if not _is_publication_finding(finding):
            continue
        for ref in finding.get("artifact_refs") or []:
            artifact_id = ref.get("artifact_id")
            if artifact_id and str(artifact_id) not in artifacts:
                _issue(issues, rule_id="RG-003", layer="STRUCTURAL", severity=BLOCKER, code="ARTIFACT_REF_NOT_FOUND",
                       message="Finding 引用了 Canonical Report artifacts 中不存在的 Artifact。", finding=finding,
                       actual=artifact_id, expected="artifact id present in payload.artifacts")

    claims = payload.get("claim_manifest") or {}
    finding_refs = {str(f.get("finding_id") or f.get("stable_key")) for f in (payload.get("findings") or [])}
    for claim in claims.get("claims") or []:
        if str(claim.get("finding_ref")) not in finding_refs:
            _issue(issues, rule_id="RG-004", layer="STRUCTURAL", severity=BLOCKER, code="CLAIM_FINDING_REF_NOT_FOUND",
                   message="Claim Manifest 中的 Finding 引用不存在。", actual=claim.get("finding_ref"), expected=sorted(finding_refs))


def _validate_semantic(payload: dict, issues: list[GroundingIssue]) -> None:
    for finding in payload.get("findings") or []:
        ftype = str(finding.get("type") or "")
        metrics = finding.get("metrics") or {}
        semantic = finding.get("semantic_summary") or {}

        if ftype == "HIGH_DELTA":
            events = metrics.get("events") or []
            all_seq = metrics.get("all_sequence_continuous")
            if all_seq is None and events:
                all_seq = all(isinstance(x, dict) and x.get("sequence_continuous") is True for x in events)
            lost = metrics.get("stream_lost_packets")
            if all_seq is True and (lost is None or float(lost) == 0.0):
                if semantic.get("loss_interpretation") != "DELAY_NOT_PACKET_LOSS":
                    _issue(issues, rule_id="RG-005", layer="SEMANTIC", severity=BLOCKER, code="HIGH_DELTA_LOSS_SEMANTIC_CONTRADICTION",
                           message="HIGH_DELTA 的 Sequence 连续且 Stream 无丢包时，必须明确解释为 Delay/Stall 而不是 Packet Loss。",
                           finding=finding, actual=semantic.get("loss_interpretation"), expected="DELAY_NOT_PACKET_LOSS")

        if ftype in {"PACKET_LOSS", "BURST_LOSS"}:
            lost_candidates = [metrics.get("lost_packets"), metrics.get("stream_lost_packets"), metrics.get("sequence_gap_packets")]
            has_positive_loss = any(isinstance(v, (int, float)) and float(v) > 0 for v in lost_candidates)
            if not has_positive_loss:
                _issue(issues, rule_id="RG-006", layer="SEMANTIC", severity=BLOCKER, code="PACKET_LOSS_WITHOUT_LOSS_EVIDENCE",
                       message="Packet Loss Finding 必须有正向 Sequence/Loss 事实，不能由 HIGH_DELTA 或文本推断。",
                       finding=finding, actual=lost_candidates, expected="one positive loss/sequence-gap metric")

        if ftype in _PERIODIC_FINDINGS:
            boundary = str(finding.get("root_cause_boundary") or "")
            if not boundary or not any(token in boundary for token in ("不能", "不等于", "不确认", "需", "cannot", "not confirm")):
                _issue(issues, rule_id="RG-007", layer="SEMANTIC", severity=BLOCKER, code="PERIODIC_ROOT_CAUSE_BOUNDARY_MISSING",
                       message="周期/工频族 Finding 必须明确声明物理 Root Cause 尚未确认。", finding=finding,
                       actual=boundary, expected="explicit preliminary/unknown physical root-cause boundary")
            if _text_has_confirmed_physical_root_cause(finding):
                _issue(issues, rule_id="RG-008", layer="SEMANTIC", severity=BLOCKER, code="PERIODIC_PHYSICAL_ROOT_CAUSE_OVERCLAIM",
                       message="周期信号证据不得直接确认电源/接地/话柄/线路/FXS-SLIC 物理根因。", finding=finding)

        first = ((finding.get("correlation") or {}).get("first_observable_boundary") or {})
        if first.get("status") == "OBSERVED_BOUNDARY" and not first.get("first_observable_layer"):
            _issue(issues, rule_id="RG-009", layer="SEMANTIC", severity=BLOCKER, code="OBSERVED_BOUNDARY_WITHOUT_LAYER",
                   message="已声明 OBSERVED_BOUNDARY 时必须给出 first_observable_layer。", finding=finding, actual=first, expected="first_observable_layer")


def _validate_evidence(payload: dict, issues: list[GroundingIssue]) -> None:
    for finding in payload.get("findings") or []:
        # Report-level visuals and safe clip inventory exist only after persistence.
        # In-memory Golden replay is still validated by semantic rules above plus
        # its own Analyzer Artifact/Card truth checks.
        if not _is_publication_finding(finding):
            continue
        ftype = str(finding.get("type") or "")
        severity = str(finding.get("severity") or "INFO").upper()
        card = finding.get("evidence_card") or {}
        if severity in {"MEDIUM", "HIGH", "CRITICAL"} and not card:
            _issue(issues, rule_id="RG-010", layer="EVIDENCE", severity=BLOCKER, code="EVIDENCE_CARD_MISSING",
                   message="MEDIUM/HIGH/CRITICAL Finding 必须有 Evidence Card。", finding=finding)
            continue

        visuals = card.get("visual_evidence") or []
        if ftype in _VISUAL_REQUIRED_FINDINGS and severity in {"MEDIUM", "HIGH", "CRITICAL"} and not visuals:
            _issue(issues, rule_id="RG-011", layer="EVIDENCE", severity=BLOCKER, code="PRIMARY_VISUAL_MISSING",
                   message="该 Finding 类型需要至少一张精确绑定的主可视化证据。", finding=finding,
                   actual=0, expected=">=1 report-safe visual")
        for visual in visuals:
            if str(visual.get("type") or "") not in _REPORT_SAFE_IMAGE_TYPES or not visual.get("annotation_complete"):
                _issue(issues, rule_id="RG-012", layer="EVIDENCE", severity=BLOCKER, code="VISUAL_ANNOTATION_INCOMPLETE",
                       message="Evidence Card 可视化必须是 Report-safe 类型且通过 Renderer annotation contract。", finding=finding,
                       actual={"type": visual.get("type"), "annotation_complete": visual.get("annotation_complete")}, expected="report-safe image + annotation_complete=true")

        audio = card.get("audio_evidence") or {}
        if ftype in _AUDIO_EXPECTED_FINDINGS:
            status = str(audio.get("status") or "")
            if status == "AVAILABLE":
                clips = audio.get("clips") or []
                if not clips or any(str(x.get("type") or "") not in _REPORT_SAFE_AUDIO_TYPES for x in clips):
                    _issue(issues, rule_id="RG-013", layer="EVIDENCE", severity=BLOCKER, code="AUDIO_STATUS_WITHOUT_SAFE_CLIP",
                           message="音频状态为 AVAILABLE 时必须至少有一个 Report-safe anomaly clip。", finding=finding,
                           actual=[x.get("type") for x in clips], expected=sorted(_REPORT_SAFE_AUDIO_TYPES))
            elif status == "UNAVAILABLE":
                if not str(audio.get("reason") or "").strip():
                    _issue(issues, rule_id="RG-014", layer="EVIDENCE", severity=BLOCKER, code="AUDIO_UNAVAILABLE_WITHOUT_REASON",
                           message="需要异常音频但不可用时必须显式说明 UNAVAILABLE 原因。", finding=finding)
                else:
                    _issue(issues, rule_id="RG-015", layer="EVIDENCE", severity=WARNING, code="AUDIO_EVIDENCE_UNAVAILABLE",
                           message="该 Finding 允许发布，但异常音频不可用，可复核性应降级。", finding=finding,
                           actual=audio.get("reason"), expected="representative anomaly clip")
            else:
                _issue(issues, rule_id="RG-016", layer="EVIDENCE", severity=BLOCKER, code="AUDIO_EXPECTED_STATUS_INVALID",
                       message="需要异常音频的 Finding 必须明确 AVAILABLE 或 UNAVAILABLE。", finding=finding, actual=status, expected="AVAILABLE|UNAVAILABLE")

        if ftype in _PACKET_REF_REQUIRED_FINDINGS and not (card.get("packet_refs") or []):
            _issue(issues, rule_id="RG-017", layer="EVIDENCE", severity=BLOCKER, code="PACKET_FRAME_TRACE_MISSING",
                   message="RTP packet anomaly Finding 必须保留 Frame/Seq 下钻证据。", finding=finding)


def _validate_explainability(payload: dict, issues: list[GroundingIssue]) -> None:
    for finding in payload.get("findings") or []:
        severity = str(finding.get("severity") or "INFO").upper()
        if severity not in {"MEDIUM", "HIGH", "CRITICAL"}:
            continue
        card = finding.get("evidence_card") or {}
        if not card and not _is_publication_finding(finding):
            continue
        for field, code, rule_id in (
            ("what_happened", "WHAT_HAPPENED_MISSING", "RG-018"),
            ("root_cause_boundary", "ROOT_CAUSE_BOUNDARY_MISSING", "RG-019"),
            ("next_action", "NEXT_ACTION_MISSING", "RG-020"),
        ):
            if not str(card.get(field) or "").strip():
                _issue(issues, rule_id=rule_id, layer="EXPLAINABILITY", severity=BLOCKER, code=code,
                       message=f"Evidence Card 缺少 {field}，无法形成可复核诊断说明。", finding=finding)
        time = card.get("time") or {}
        if not time.get("representative_utc") and not time.get("absolute_start_utc"):
            _issue(issues, rule_id="RG-021", layer="EXPLAINABILITY", severity=WARNING, code="HUMAN_READABLE_TIME_UNAVAILABLE",
                   message="Finding 没有可读绝对时间，报告仍可发布但复核效率下降。", finding=finding)

    comparisons = payload.get("ab_comparison")
    if isinstance(comparisons, list) and not comparisons and payload.get("ab_conclusion"):
        _issue(issues, rule_id="RG-022", layer="EXPLAINABILITY", severity=BLOCKER, code="AB_CONCLUSION_WITHOUT_COMPARISON",
               message="没有 A/B Comparison 数据时不得输出 A/B 结论。", actual=payload.get("ab_conclusion"), expected=None)


def _derive_reviewability(payload: dict, issues: list[GroundingIssue]) -> str:
    if any(x.severity == BLOCKER for x in issues):
        return "NOT_REVIEWABLE"
    completeness = payload.get("completeness") or {}
    prior = str(completeness.get("reviewability") or "")
    if prior in {"NOT_FULLY_REVIEWABLE", "NOT_REVIEWABLE"}:
        return "PARTIALLY_REVIEWABLE"
    if str(completeness.get("state") or "") != "COMPLETE":
        return "PARTIALLY_REVIEWABLE"
    if any(x.severity == WARNING for x in issues):
        return "PARTIALLY_REVIEWABLE"
    return "FULLY_REVIEWABLE"


def validate_report_grounding(payload: dict) -> dict:
    """Validate canonical report truth without changing Analyzer/Finding results."""
    issues: list[GroundingIssue] = []
    _validate_structural(payload, issues)
    _validate_semantic(payload, issues)
    _validate_evidence(payload, issues)
    _validate_explainability(payload, issues)
    counts = Counter(x.severity for x in issues)
    status = FAIL if counts.get(BLOCKER, 0) else PASS_WITH_WARNINGS if counts.get(WARNING, 0) else PASS
    reviewability = _derive_reviewability(payload, issues)
    findings = payload.get("findings") or []
    publication_findings = sum(1 for finding in findings if _is_publication_finding(finding))
    return {
        "schema_version": GROUNDING_VALIDATOR_VERSION,
        "status": status,
        "reviewability_status": reviewability,
        "validation_scope": "PUBLICATION" if publication_findings or not findings else "IN_MEMORY_REPLAY",
        "publication_finding_count": publication_findings,
        "counts": {"blockers": counts.get(BLOCKER, 0), "warnings": counts.get(WARNING, 0), "issues": len(issues)},
        "issues": [asdict(x) for x in issues],
        "policy": {
            "block_publish_on": BLOCKER,
            "warning_behavior": "publish allowed but reviewability is PARTIALLY_REVIEWABLE",
            "authority": "Analyzer/Finding structured facts are authoritative; validator does not create new root-cause truth.",
        },
    }


def apply_report_grounding(payload: dict, *, raise_on_blocker: bool = False) -> dict:
    payload["claim_manifest"] = build_claim_manifest(payload)
    validation = validate_report_grounding(payload)
    payload["grounding_validation"] = validation
    payload["reviewability_status"] = validation["reviewability_status"]
    completeness = payload.setdefault("completeness", {})
    completeness["reviewability"] = validation["reviewability_status"]
    completeness["grounding_status"] = validation["status"]
    if validation["status"] == FAIL:
        completeness["state"] = "PARTIAL"
        prior = str(completeness.get("boundary") or "")
        completeness["boundary"] = ("Report Grounding Validator 检测到发布阻断级矛盾；该报告不可作为可复核初步证据报告发布。 " + prior).strip()
        if raise_on_blocker:
            raise ReportGroundingError(validation)
    return validation


def grounding_json(payload: dict) -> str:
    clone = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    apply_report_grounding(clone, raise_on_blocker=False)
    return json.dumps(clone.get("grounding_validation") or {}, ensure_ascii=False, indent=2)

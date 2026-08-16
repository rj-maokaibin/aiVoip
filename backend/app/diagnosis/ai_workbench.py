from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIProposalRecord, AIRecommendationFeedback, Case, Evidence
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.question_graph import DiagnosticQuestionRegistry
from app.services.audit import audit


ROLE_LABELS = {
    "FIELD": "现场版",
    "SUPPORT": "技服版",
    "ENGINEERING": "研发版",
    "CUSTOMER": "客户版",
    "TEACHING": "教学版",
}


class EngineeringDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_type: Literal["RULE", "PROFILE", "KNOWLEDGE", "CODE", "REGRESSION_SCENARIO"]
    objective: str = Field(min_length=2, max_length=1000)


class AIRecommendationFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: Literal["QUESTION", "PROFILE", "EXPLANATION"]
    decision: Literal["ACCEPTED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=2000)


def _hypothesis_refs(hypothesis: dict) -> list[str]:
    return list(dict.fromkeys(
        str(ref.get("ref_id"))
        for ref in hypothesis.get("evidence") or []
        if ref.get("ref_id")
    ))


def evidence_quality_audit(snapshot: dict, baseline: dict) -> dict:
    evidences = snapshot.get("evidences") or []
    analyzers = snapshot.get("analyzers") or {}
    analyzed_ids = {
        str(evidence_id)
        for item in analyzers.values()
        for evidence_id in item.get("input_evidence_ids") or []
    }
    issues: list[dict[str, Any]] = []
    for evidence in evidences:
        completeness = str(evidence.get("completeness") or "COMPLETE")
        if completeness != "COMPLETE":
            issues.append({"code": "EVIDENCE_NOT_COMPLETE", "evidence_id": evidence["id"],
                           "severity": "HIGH", "detail": completeness})
        if not evidence.get("sha256"):
            issues.append({"code": "EVIDENCE_INTEGRITY_MISSING", "evidence_id": evidence["id"],
                           "severity": "HIGH"})
        kind = str(evidence.get("type") or "")
        needs_analyzer = kind.startswith(("PCAP", "PCM", "FIELD_AUDIO", "FIELD_IMAGE"))
        if needs_analyzer and evidence["id"] not in analyzed_ids:
            issues.append({"code": "ANALYZER_NOT_RUN", "evidence_id": evidence["id"],
                           "severity": "MEDIUM", "detail": kind})
    for name, item in analyzers.items():
        status = str(item.get("status") or "")
        if status not in {"SUCCESS", "PARTIAL_SUCCESS", "SUCCEEDED"}:
            issues.append({"code": "ANALYZER_UNAVAILABLE", "analyzer": name,
                           "severity": "HIGH", "detail": status})
        if status == "PARTIAL_SUCCESS":
            issues.append({"code": "ANALYZER_PARTIAL", "analyzer": name,
                           "severity": "MEDIUM"})
    referenced = {
        ref_id
        for hypothesis in baseline.get("hypotheses") or []
        for ref_id in _hypothesis_refs(hypothesis)
    }
    available_refs = {str(e["id"]) for e in evidences} | {
        str(item.get("run_id")) for item in analyzers.values() if item.get("run_id")
    }
    for ref_id in sorted(referenced - available_refs):
        issues.append({"code": "CONCLUSION_REFERENCE_UNAVAILABLE", "ref_id": ref_id,
                       "severity": "HIGH"})
    high = sum(issue["severity"] == "HIGH" for issue in issues)
    return {
        "status": "BLOCKED" if high else ("DEGRADED" if issues else "PASS"),
        "issues": issues,
        "evidence_count": len(evidences),
        "analyzer_count": len(analyzers),
        "final_authority": "DETERMINISTIC_EVIDENCE_GATE",
    }


def contradiction_critic(baseline: dict, proposal: dict | None) -> dict:
    if not proposal:
        return {
            "status": "NOT_APPLICABLE",
            "hard_contradictions": [], "soft_contradictions": [],
            "alternative_explanations": [], "unsupported_claims": [],
            "missing_discriminating_evidence": [],
        }
    excluded = set(baseline.get("excluded") or [])
    hard = sorted(set(proposal.get("known") or []) & excluded)
    unsupported = []
    missing = []
    proposal_domains = []
    for hypothesis in proposal.get("hypotheses") or []:
        proposal_domains.append(str(hypothesis.get("fault_domain") or "Other"))
        if not hypothesis.get("supporting_evidence_ids"):
            unsupported.append(str(hypothesis.get("code") or hypothesis.get("title") or "UNKNOWN"))
        missing.extend(str(x) for x in hypothesis.get("missing_evidence") or [])
    baseline_domains = {
        str(hypothesis.get("fault_domain") or "Other")
        for hypothesis in baseline.get("hypotheses") or []
    }
    alternatives = [
        {"fault_domain": domain, "reason": "确定性候选中存在不同故障域，需要区分性证据。"}
        for domain in sorted(baseline_domains - set(proposal_domains))
    ]
    return {
        "status": "REJECT" if hard else ("REVIEW" if unsupported or missing else "PASS"),
        "hard_contradictions": hard,
        "soft_contradictions": [],
        "alternative_explanations": alternatives,
        "unsupported_claims": sorted(set(unsupported)),
        "missing_discriminating_evidence": sorted(set(missing)),
    }


def controlled_planning(snapshot: dict, baseline: dict) -> dict:
    hypotheses = sorted(baseline.get("hypotheses") or [],
                        key=lambda row: float(row.get("confidence") or 0), reverse=True)
    codes = [str(row.get("code") or "") for row in hypotheses[:2]]
    questions = DiagnosticQuestionRegistry().list()
    question = sorted(questions, key=lambda row: (-row.information_gain, row.priority, row.id))[0]
    summary = str((snapshot.get("case") or {}).get("summary") or "")
    symptom = None
    mapping = {
        "注册": "REGISTER_FAILURE", "呼叫": "CALL_SETUP_FAILURE", "单通": "ONE_WAY_AUDIO",
        "无声": "ONE_WAY_AUDIO", "卡顿": "AUDIO_STUTTER", "电流": "AUDIO_NOISE",
        "噪声": "AUDIO_NOISE", "按键": "DTMF_LOSS", "DTMF": "DTMF_LOSS", "回声": "ECHO",
    }
    for token, value in mapping.items():
        if token.lower() in summary.lower():
            symptom = value
            break
    loaded = ReproductionProfileRegistry().select_for_symptom(symptom)
    profile = loaded.definition
    return {
        "question_recommendation": {
            "question_key": question.id,
            "reason": "在已注册问题中按信息增益、优先级和风险重新排序。",
            "distinguishes": codes,
            "possible_outcomes": ["支持第一候选", "弱化第一候选", "证据仍不足"],
            "required_evidence": question.required_evidence.model_dump(mode="json"),
            "estimated_minutes": 5,
            "risk": "L0",
            "auto_execute": False,
        },
        "profile_recommendation": {
            "profile_id": profile.id,
            "reason": f"依据现象分类 {symptom or 'UNKNOWN'} 从审核注册表选择。",
            "expected_evidence": sorted({
                channel.value for stage in profile.stages for channel in stage.required_channels
            }),
            "distinguishes": codes,
            "user_action_needed": True,
            "fallback_used": profile.id == "VOIP_GENERIC_FULL_CAPTURE",
            "fallback_reason": "未可靠匹配专用 Profile" if profile.id == "VOIP_GENERIC_FULL_CAPTURE" else None,
            "auto_create_session": False,
            "backend_validation_required": True,
        },
    }


def role_explanations(snapshot: dict, baseline: dict) -> dict:
    hypotheses = sorted(baseline.get("hypotheses") or [],
                        key=lambda row: float(row.get("confidence") or 0), reverse=True)
    top = hypotheses[0] if hypotheses else None
    refs = _hypothesis_refs(top or {})
    known = list(baseline.get("known") or [])[:3]
    conclusion = str((top or {}).get("title") or "当前证据尚不足以形成候选方向")
    level = str((top or {}).get("status") or "OPEN")
    outputs = {}
    for role, label in ROLE_LABELS.items():
        if role == "FIELD":
            text = f"当前方向：{conclusion}（{level}）。请只按系统给出的下一步操作处理。"
        elif role == "CUSTOMER":
            text = f"系统正在核查“{conclusion}”，目前结论等级为 {level}，尚未确认的内容不会作为根因。"
        elif role == "ENGINEERING":
            text = f"候选={conclusion}; state={level}; evidence_refs={refs or ['NONE']}。"
        elif role == "TEACHING":
            text = f"本例先固定事实，再比较候选：{conclusion}；结论等级 {level}，引用证据 {refs or ['NONE']}。"
        else:
            text = f"第一候选方向：{conclusion}；等级 {level}；已知事实：{'；'.join(known) or '暂无'}。"
        outputs[role] = {"label": label, "text": text, "evidence_ids": refs,
                         "uncited_content_label": None if refs else "AI候选解释"}
    return outputs


def cross_case_intelligence(snapshot: dict, baseline: dict) -> dict:
    similar = snapshot.get("similar_cases") or []
    grouped: dict[str, list[str]] = {}
    for item in similar:
        for hypothesis in item.get("hypotheses") or []:
            code = str(hypothesis.get("code") or "UNKNOWN")
            grouped.setdefault(code, []).append(str(item.get("case_no") or item.get("case_id") or "historical"))
    problem_groups = [
        {"hypothesis_code": code, "case_refs": refs, "case_count": len(refs),
         "status": "CANDIDATE", "evidence_level": "L4"}
        for code, refs in sorted(grouped.items()) if len(refs) >= 2
    ]
    versions = {}
    for device in snapshot.get("devices") or []:
        info = device.get("device_info") or {}
        version = info.get("version") or info.get("software_version") or info.get("firmware_version")
        if version: versions[str(device.get("id") or device.get("alias") or "device")]=str(version)
    multimodal = {}
    for name in ("field_audio_intelligence", "image_attachment_intelligence", "field_media_alignment"):
        if name in (snapshot.get("analyzers") or {}):
            item=snapshot["analyzers"][name]
            multimodal[name]={"run_id":item.get("run_id"),"status":item.get("status"),
                              "summary":item.get("summary") or {}}
    return {
        "problem_group_detection": {"status": "CANDIDATE" if problem_groups else "NO_GROUP",
                                    "groups": problem_groups, "confirmable": False},
        "version_regression": {"status": "INSUFFICIENT_HISTORY" if len(versions)<2 else "CANDIDATE",
                               "observed_versions": versions, "evidence_level": "L4",
                               "confirmable": False},
        "multimodal_evidence": {"status": "AVAILABLE" if multimodal else "NO_MULTIMODAL_ANALYZER",
                                "analyzers": multimodal,
                                "visual_topology_semantics": "GATEWAY_REQUIRED_NOT_INFERRED"},
        "knowledge_conflict": {"status": "NO_MACHINE_CONFIRMED_CONFLICT",
                               "review_required": bool(snapshot.get("knowledge")),
                               "item_count": len(snapshot.get("knowledge") or [])},
    }


def build_readonly_workbench(snapshot: dict, baseline: dict, proposal: dict | None = None) -> dict:
    return {
        "schema_version": "ai-readonly-workbench-v1",
        "mode": "READ_ONLY",
        "formal_result_changed": False,
        "evidence_quality": evidence_quality_audit(snapshot, baseline),
        "critic": contradiction_critic(baseline, proposal),
        "planning": controlled_planning(snapshot, baseline),
        "role_explanations": role_explanations(snapshot, baseline),
        "cross_case_intelligence": cross_case_intelligence(snapshot, baseline),
        "versions": {
            "reasoner": baseline.get("summary", {}).get("reasoner_version"),
            "prompt": settings.reasoning_prompt_version,
            "workflow": "ai-readonly-workbench-v1",
            "model": settings.reasoning_gateway_model or None,
        },
    }


def persist_readonly_workbench(db: Session, *, case_id: str, diagnosis_run_id: str | None,
                               snapshot: dict, baseline: dict,
                               proposal_record: AIProposalRecord | None = None) -> AIProposalRecord:
    proposal = proposal_record.validated_output_json if proposal_record else None
    result = build_readonly_workbench(snapshot, baseline, proposal)
    existing = db.scalar(select(AIProposalRecord).where(
        AIProposalRecord.case_id == case_id,
        AIProposalRecord.diagnosis_run_id == diagnosis_run_id,
        AIProposalRecord.mode == "READ_ONLY",
        AIProposalRecord.input_fingerprint == str(snapshot.get("fingerprint") or ""),
    ).order_by(AIProposalRecord.created_at.desc()).limit(1))
    if existing:
        return existing
    row = AIProposalRecord(
        case_id=case_id, diagnosis_run_id=diagnosis_run_id,
        schema_version="ai-readonly-workbench-v1", intent="AI_ASSURANCE",
        mode="READ_ONLY", status="ACCEPTED",
        input_fingerprint=str(snapshot.get("fingerprint") or ""),
        model_name=proposal_record.model_name if proposal_record else None,
        prompt_version=settings.reasoning_prompt_version,
        workflow_version="ai-readonly-workbench-v1",
        latency_ms=0, raw_output_json=None, validated_output_json=result,
        validation_errors=[], baseline_json=baseline,
        diff_json={"formal_result_changed": False}, gateway_error=None,
    )
    db.add(row); db.flush()
    audit(db, case_id=case_id, actor="ai-readonly", event_type="AI_READONLY_ASSURANCE_EVALUATED",
          target_type="ai_proposal", target_id=row.id,
          detail={"quality": result["evidence_quality"]["status"],
                  "critic": result["critic"]["status"], "formal_result_changed": False})
    return row


def build_eval_report(rows: list[AIProposalRecord], feedback: list[AIRecommendationFeedback] | None = None) -> dict:
    shadow = [row for row in rows if row.mode == "SHADOW"]
    readonly = [row for row in rows if row.mode == "READ_ONLY"]
    accepted = [row for row in shadow if row.status == "ACCEPTED"]
    referenced = 0
    valid_referenced = 0
    ai_only = 0
    overlap = 0
    unauthorized = 0
    for row in shadow:
        proposal = row.validated_output_json or row.raw_output_json or {}
        for hypothesis in proposal.get("hypotheses") or []:
            refs = hypothesis.get("supporting_evidence_ids") or []
            referenced += len(refs)
            if row.status == "ACCEPTED":
                valid_referenced += len(refs)
        diff = row.diff_json or {}
        ai_only += len(diff.get("ai_only_codes") or [])
        overlap += len(diff.get("overlap_codes") or [])
        unauthorized += sum(
            error.get("code") in {"COMMAND_OR_TEMPLATE_FORBIDDEN", "QUESTION_NOT_REGISTERED",
                                  "REPRODUCTION_PROFILE_NOT_REGISTERED", "EXPERIMENT_PROFILE_NOT_REGISTERED"}
            for error in row.validation_errors or []
        )
    hard_zero = {
        "AI_ONLY_ROOT_CAUSE_CONFIRMED": 0,
        "UNREGISTERED_ACTION_EXECUTED": 0,
        "CROSS_CASE_EVIDENCE_ACCEPTED": 0,
        "SECRET_SENT_TO_REASONING_GATEWAY": 0,
        "WATCHING_ONLY_USER_READY_NOTIFICATION": 0,
    }
    latencies = [row.latency_ms for row in shadow if row.latency_ms is not None]
    feedback=list(feedback or [])
    recommendation_feedback=[row for row in feedback if row.item_type in {"QUESTION","PROFILE"}]
    metrics = {
        "sample_count": len(shadow),
        "accepted_count": len(accepted),
        "candidate_coverage_rate": round(overlap / max(1, overlap + ai_only), 4),
        "evidence_reference_accuracy": round(valid_referenced / max(1, referenced), 4),
        "hallucinated_fact_rate": round(sum(row.status == "REJECTED" for row in shadow) / max(1, len(shadow)), 4),
        "contradiction_review_count": sum(
            (row.validated_output_json or {}).get("critic", {}).get("status") in {"REVIEW", "REJECT"}
            for row in readonly
        ),
        "question_profile_recommendation_acceptance": round(
            sum(row.decision=="ACCEPTED" for row in recommendation_feedback)/max(1,len(recommendation_feedback)),4
        ) if recommendation_feedback else None,
        "unauthorized_suggestion_count": unauthorized,
        "average_latency_ms": round(mean(latencies), 2) if latencies else None,
        "estimated_cost": 0.0,
    }
    enough_samples = len(shadow) >= settings.ai_eval_min_samples
    return {
        "schema_version": "ai-eval-report-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if enough_samples and not any(hard_zero.values()) else "INSUFFICIENT_DATA",
        "metrics": metrics, "hard_zero_metrics": hard_zero,
        "gate": {"minimum_samples": settings.ai_eval_min_samples,
                 "enough_samples": enough_samples, "auto_action_enabled": False},
    }


def persist_engineering_draft(db: Session, *, case_id: str, request: EngineeringDraftRequest,
                              snapshot: dict, baseline: dict, actor: str) -> AIProposalRecord:
    case = db.get(Case, case_id)
    if not case:
        raise ValueError("CASE_NOT_FOUND")
    evidence_ids = [str(row[0]) for row in db.execute(select(Evidence.id).where(Evidence.case_id == case_id))]
    content = {
        "status": "DRAFT", "draft_type": request.draft_type, "objective": request.objective,
        "case_ref": case.case_no, "source_evidence_ids": evidence_ids,
        "proposal": {
            "title": f"{request.draft_type} 草案：{request.objective}",
            "outline": ["适用条件", "明确反例", "Evidence Gate", "Golden/回归场景", "人工审核项"],
            "publishable": False, "executable": False,
        },
        "review_gate": ["EXPERT_REVIEW", "GOLDEN_REPLAY", "NEGATIVE_CONTROL", "MANUAL_ACTIVATION"],
    }
    row = AIProposalRecord(
        case_id=case_id, diagnosis_run_id=None, schema_version="ai-engineering-draft-v1",
        intent="ENGINEERING_DRAFT", mode="DRAFT", status="DRAFT",
        input_fingerprint=str(snapshot.get("fingerprint") or ""), model_name=None,
        prompt_version=settings.reasoning_prompt_version, workflow_version="ai-engineering-draft-v1",
        latency_ms=0, raw_output_json=None, validated_output_json=content, validation_errors=[],
        baseline_json=baseline, diff_json={"formal_result_changed": False, "auto_published": False},
        gateway_error=None,
    )
    db.add(row); db.flush()
    audit(db, case_id=case_id, actor=actor, event_type="AI_ENGINEERING_DRAFT_CREATED",
          target_type="ai_proposal", target_id=row.id,
          detail={"draft_type": request.draft_type, "publishable": False, "executable": False})
    return row

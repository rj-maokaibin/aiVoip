from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.reproduction.question_graph import DiagnosticQuestionRegistry, DiagnosticQuestionTemplate


_SYMPTOM_QUESTION = {
    "REGISTER_FAILURE": "REGISTER_PATH_FAILURE_DOMAIN",
    "CALL_SETUP_FAILURE": "CALL_SETUP_FAILURE_LAYER",
    "ONE_WAY_AUDIO": "ONE_WAY_FAULT_LAYER",
    "AUDIO_STUTTER": "STUTTER_FAULT_LAYER",
    "AUDIO_NOISE": "AUDIO_NOISE_FAULT_LAYER",
    "DTMF_LOSS": "DTMF_FIRST_MISMATCH_LAYER",
    "ECHO": "ECHO_FAULT_LAYER",
}

_SYMPTOM_TOKENS = {
    "REGISTER_FAILURE": ("注册", "register"),
    "CALL_SETUP_FAILURE": ("呼叫", "invite", "回铃"),
    "ONE_WAY_AUDIO": ("单通", "无声", "one way"),
    "AUDIO_STUTTER": ("卡顿", "断续", "jitter", "stutter"),
    "AUDIO_NOISE": ("电流", "噪声", "noise", "hum"),
    "DTMF_LOSS": ("dtmf", "丢号", "按键"),
    "ECHO": ("回声", "echo", "啸叫"),
}


@dataclass(frozen=True)
class PlannedQuestion:
    question_key: str
    score: float
    reason: str
    distinguishes: list[str]
    required_evidence: dict
    missing_findings: list[str]
    estimated_minutes: int
    risk: str
    experiment_profiles: list[str]

    def to_dict(self) -> dict:
        return {
            "question_key": self.question_key,
            "score": round(self.score, 4),
            "reason": self.reason,
            "distinguishes": self.distinguishes,
            "required_evidence": self.required_evidence,
            "missing_findings": self.missing_findings,
            "estimated_minutes": self.estimated_minutes,
            "risk": self.risk,
            "experiment_profiles": self.experiment_profiles,
            "auto_execute": False,
        }


def infer_symptom(summary: str) -> str | None:
    lowered = (summary or "").lower()
    for symptom, tokens in _SYMPTOM_TOKENS.items():
        if any(token.lower() in lowered for token in tokens):
            return symptom
    return None


def _normalize_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", text or "")
        if token
    }


def _observed_findings(snapshot: dict) -> set[str]:
    findings: set[str] = set()
    for analyzer in (snapshot.get("analyzers") or {}).values():
        for container_key in ("summary", "result"):
            container = analyzer.get(container_key) or {}
            if isinstance(container, dict):
                for key, value in container.items():
                    if key == "findings" and isinstance(value, list):
                        findings.update(str(item) for item in value)
                    elif isinstance(value, bool) and value:
                        findings.add(str(key).upper())
        findings.update(str(item) for item in analyzer.get("findings") or [])
    return findings


def _relevance(question: DiagnosticQuestionTemplate, hypotheses: list[dict], symptom: str | None) -> float:
    score = 0.0
    if symptom and _SYMPTOM_QUESTION.get(symptom) == question.id:
        score += 1.8
    question_tokens = _normalize_tokens(question.id + " " + question.title)
    for row in hypotheses[:3]:
        tokens = _normalize_tokens(
            f"{row.get('code', '')} {row.get('fault_domain', '')} {row.get('title', '')}"
        )
        overlap = len(question_tokens & tokens)
        if overlap:
            score += min(1.2, 0.35 * overlap)
    if question.id == "GENERIC_SYMPTOM_CLASSIFICATION" and symptom:
        score -= 1.0
    return score


def rank_questions(
    snapshot: dict,
    baseline: dict,
    *,
    registry: DiagnosticQuestionRegistry | None = None,
) -> list[PlannedQuestion]:
    registry = registry or DiagnosticQuestionRegistry()
    hypotheses = sorted(
        baseline.get("hypotheses") or [],
        key=lambda row: float(row.get("confidence") or 0.0),
        reverse=True,
    )
    candidate_codes = [str(row.get("code") or "") for row in hypotheses[:3] if row.get("code")]
    summary = str((snapshot.get("case") or {}).get("summary") or "")
    symptom = infer_symptom(summary)
    observed = _observed_findings(snapshot)

    ranked: list[PlannedQuestion] = []
    for question in registry.list():
        required = question.required_evidence.model_dump(mode="json")
        missing = sorted(set(question.required_evidence.must_findings) - observed)
        relevance = _relevance(question, hypotheses, symptom)
        # Information gain is the primary deterministic prior; relevance makes it
        # conditional on the current competing hypotheses instead of globally picking
        # the same question every time. Missing evidence lowers immediate answerability
        # but does not eliminate the question because it can define the next collection.
        score = (
            float(question.information_gain)
            + relevance * 60.0
            - float(question.priority) * 0.25
            - len(missing) * 12.0
        )
        level = str(getattr(question.level, "value", question.level))
        estimated_minutes = 3 + len(missing) * 3 + (4 if question.experiment_profiles else 0)
        risk = "L1" if question.experiment_profiles else "L0"
        reason = (
            f"针对当前候选 {candidate_codes or ['UNKNOWN']} 计算区分度；"
            f"symptom={symptom or 'UNKNOWN'}，information_gain={question.information_gain}，"
            f"missing_findings={missing or ['NONE']}，level={level}。"
        )
        ranked.append(PlannedQuestion(
            question_key=question.id,
            score=score,
            reason=reason,
            distinguishes=candidate_codes,
            required_evidence=required,
            missing_findings=missing,
            estimated_minutes=estimated_minutes,
            risk=risk,
            experiment_profiles=list(question.experiment_profiles),
        ))
    ranked.sort(key=lambda row: (-row.score, row.question_key))
    return ranked


def select_question(snapshot: dict, baseline: dict) -> PlannedQuestion:
    ranked = rank_questions(snapshot, baseline)
    if not ranked:
        raise ValueError("DIAGNOSTIC_QUESTION_REGISTRY_EMPTY")
    return ranked[0]

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_TERMINAL_BLOCKERS = {"MAX_CYCLES", "NO_PROGRESS"}
_UNAVAILABLE_STATES = {"ANSWERED", "UNKNOWN_BY_USER", "UNAVAILABLE", "DECLINED", "NOT_APPLICABLE"}

# Higher means the answer can materially reduce the diagnosis search space.
_INFORMATION_GAIN = {
    "anomaly_timestamp": 1.00,
    "pcap": 0.95,
    "recording": 0.82,
    "device_access": 0.78,
    "reproducibility": 0.70,
}

# Lower user effort is preferred when information gain is similar.
_ACQUISITION_COST = {
    "anomaly_timestamp": 0.10,
    "reproducibility": 0.15,
    "recording": 0.45,
    "pcap": 0.60,
    "device_access": 0.65,
}


@dataclass(frozen=True)
class QuestionPlan:
    kind: str
    need: str | None
    question: str | None
    reason: str
    fallback: str | None
    score: float = 0.0


def normalize_need(raw: str) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if "timestamp" in value or value in {"time", "anomaly_time"}:
        return "anomaly_timestamp"
    if "recording" in value or "audio" in value:
        return "recording"
    if "pcap" in value or "capture" in value:
        return "pcap"
    if value in {"device", "device_url", "device_or_pcap", "device_access"} or "device" in value:
        return "device_access"
    if "repro" in value:
        return "reproducibility"
    return None


def question_for_need(need: str) -> tuple[str, str]:
    if need == "anomaly_timestamp":
        return (
            "请提供本次异常发生的大致时间；如果不知道，请直接回复“不知道”。",
            "如果时间不清楚，我会把它记为未知并改用整段证据继续判断，不会重复追问。",
        )
    if need == "pcap":
        return (
            "如果方便，请上传包含异常过程的 PCAP/PCAPNG；如果暂时无法抓取，请回复“暂时不能”。",
            "如果暂时不能抓包，我会优先使用现有日志、录音或已有媒体证据。",
        )
    if need == "recording":
        return (
            "如果有异常时的现场录音，请上传；如果没有录音，请回复“没有”。",
            "没有录音也可以继续，我会明确标出无法验证的主观听感边界。",
        )
    if need == "device_access":
        return (
            "如果可以，请提供设备入口（URL，或 IP+SN）；暂时不能提供时也可以直接告诉我。",
            "没有设备入口时，我会只基于已有离线证据分析，不会自动执行设备动作。",
        )
    return (
        "这个故障现在还能复现吗？请回复：可以 / 暂时不能 / 不确定。",
        "如果暂时不能复现，我会按现有证据先给阶段结论。",
    )


def _candidate_needs(decision: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for action in decision.get("plan") or []:
        if str(action.get("action_type") or "") != "REQUEST_USER_EVIDENCE":
            continue
        params = action.get("params") or {}
        raw_needs = params.get("need") or []
        if isinstance(raw_needs, str):
            raw_needs = [raw_needs]
        for raw in raw_needs:
            need = normalize_need(str(raw))
            if need and need not in out:
                out.append(need)
    return out


def select_user_question(
    *,
    decision: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    slots: dict[str, Any] | None = None,
    unavailable_needs: list[str] | None = None,
) -> QuestionPlan:
    """Choose at most one user question from deterministic allowed needs.

    This planner never invents a diagnostic action. It ranks only needs already
    emitted by the deterministic DiagnosisDecision, suppresses needs the user has
    answered/cannot provide, and prefers high information gain with lower field
    acquisition cost.
    """
    decision = decision or {}
    summary = summary or {}
    slots = slots or {}
    unavailable = {str(x) for x in (unavailable_needs or [])}
    blocker = str(summary.get("blocking_reason") or "").upper()
    if blocker in _TERMINAL_BLOCKERS:
        return QuestionPlan(
            kind="PARTIAL_CONCLUSION",
            need=None,
            question=None,
            reason=blocker,
            fallback="按现有证据形成阶段结论；后续有新的直接证据时再继续。",
        )

    candidates = _candidate_needs(decision)
    if not candidates:
        # Reproducibility is the bounded generic fallback only when the reasoner
        # did not expose a concrete evidence need.
        candidates = ["reproducibility"]

    ranked: list[tuple[float, str]] = []
    for need in candidates:
        state = str((slots.get(need) or {}).get("state") or "UNASKED")
        if need in unavailable or state in _UNAVAILABLE_STATES:
            continue
        asked_count = int((slots.get(need) or {}).get("asked_count") or 0)
        repeat_penalty = min(0.60, asked_count * 0.30)
        score = _INFORMATION_GAIN.get(need, 0.50) - _ACQUISITION_COST.get(need, 0.50) * 0.35 - repeat_penalty
        ranked.append((score, need))

    if not ranked:
        return QuestionPlan(
            kind="PARTIAL_CONCLUSION",
            need=None,
            question=None,
            reason="NO_ASKABLE_NEED",
            fallback="当前没有值得重复追问的信息；请按现有证据形成阶段结论，或等待新的直接证据。",
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    score, need = ranked[0]
    question, fallback = question_for_need(need)
    return QuestionPlan(
        kind="QUESTION",
        need=need,
        question=question,
        reason="HIGHEST_INFORMATION_GAIN_AVAILABLE_NEED",
        fallback=fallback,
        score=round(score, 4),
    )

from __future__ import annotations

from typing import Any, Mapping


ALLOWED_AI_FIELDS = {
    "language_summary",
    "interpretation",
    "hypotheses",
    "next_experiments",
}

FORBIDDEN_FACT_FIELDS = {
    "call_state",
    "state",
    "call_end_time",
    "termination",
    "timestamps",
    "media_observation_window",
    "packet_loss",
    "lost_packets",
    "problem_count",
    "visibility",
    "evidence_level",
    "severity",
    "root_cause",
    "root_cause_status",
    "root_cause_confirmed",
    "semantic_validation",
}


class AIAuthorityViolation(ValueError):
    pass


def sanitize_ai_report_output(output: Mapping[str, Any]) -> dict[str, Any]:
    """Accept interpretation-only AI output and reject canonical fact writes.

    Unknown top-level fields are rejected rather than silently ignored so model
    schema drift cannot become an accidental fact-writing channel.
    """

    keys = {str(key) for key in output.keys()}
    forbidden = sorted(keys & FORBIDDEN_FACT_FIELDS)
    unknown = sorted(keys - ALLOWED_AI_FIELDS - FORBIDDEN_FACT_FIELDS)
    if forbidden or unknown:
        raise AIAuthorityViolation(
            f"AI_REPORT_AUTHORITY_VIOLATION:forbidden={forbidden};unknown={unknown}"
        )

    sanitized = {key: output[key] for key in ALLOWED_AI_FIELDS if key in output}
    sanitized["authority"] = "INTERPRETATION_ONLY"
    sanitized["root_cause_confirmed"] = False

    hypotheses = sanitized.get("hypotheses")
    if hypotheses is not None:
        if not isinstance(hypotheses, list):
            raise AIAuthorityViolation("AI_REPORT_HYPOTHESES_MUST_BE_LIST")
        normalized = []
        for item in hypotheses:
            if isinstance(item, Mapping):
                candidate = dict(item)
            else:
                candidate = {"statement": str(item)}
            candidate["status"] = "CANDIDATE"
            candidate["root_cause_confirmed"] = False
            normalized.append(candidate)
        sanitized["hypotheses"] = normalized

    return sanitized


def canonical_fact_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project the immutable fact subset sent to an explanation model."""

    validation = report.get("semantic_validation") or {}
    return {
        "schema": report.get("schema"),
        "semantic_validation": {
            "status": validation.get("status"),
            "ruleset": validation.get("ruleset"),
        },
        "call_reconstruction": report.get("call_reconstruction") or {},
        "timeline": report.get("timeline") or {},
        "visibility": report.get("visibility") or {},
        "problem_count": report.get("problem_count"),
        "events": report.get("events") or [],
        "findings": report.get("findings") or [],
        "correlation_clusters": report.get("correlation_clusters") or [],
        "normal_evidence": report.get("normal_evidence") or [],
        "exclusion_evidence": report.get("exclusion_evidence") or [],
    }


def ai_explanation_allowed(report: Mapping[str, Any]) -> bool:
    validation = report.get("semantic_validation") or {}
    return validation.get("status") == "PASS" and report.get("publishable") is True

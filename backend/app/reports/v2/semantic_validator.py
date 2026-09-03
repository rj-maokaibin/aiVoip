from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class SemanticViolation:
    rule: str
    severity: str
    path: str
    detail: str


def validate_foundation_semantics(
    *,
    call: Mapping[str, Any],
    timeline: Mapping[str, Any],
    rtp_streams: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fail-closed validator for the first V2 lifecycle/timeline invariants.

    This foundation validator intentionally covers only the rules implemented by
    the first code slice. It is not the final R001-R015 release validator; later
    phases extend the same contract rather than silently changing these rules.
    """

    violations: list[SemanticViolation] = []
    termination = call.get("termination") or {}
    call_end = call.get("call_end_time")

    if call_end is not None and termination.get("observed") is not True:
        violations.append(
            SemanticViolation(
                rule="R001",
                severity="P0",
                path="call.call_end_time",
                detail="Call end exists without an observed protocol termination/failure event.",
            )
        )

    observed_rtp = [stream for stream in rtp_streams if int(stream.get("packet_count") or 0) > 0]
    media = timeline.get("media_observation_window") or {}
    media_start = media.get("start")
    media_end = media.get("end")

    if observed_rtp:
        invalid_window = (
            media_start is None
            or media_end is None
            or float(media_end) <= float(media_start)
        )
        if invalid_window:
            violations.append(
                SemanticViolation(
                    rule="R002",
                    severity="P0",
                    path="timeline.media_observation_window",
                    detail="Observed RTP exists but the aggregate media window is missing or zero/negative length.",
                )
            )

        if media.get("source") != "RTP_OBSERVATION":
            violations.append(
                SemanticViolation(
                    rule="R003",
                    severity="P0",
                    path="timeline.media_observation_window.source",
                    detail="Observed RTP exists but media timing is not sourced from RTP observation facts.",
                )
            )

    return {
        "status": "FAIL" if violations else "PASS",
        "ruleset": "preliminary-evidence-v2-foundation-r001-r003",
        "violations": [asdict(item) for item in violations],
    }

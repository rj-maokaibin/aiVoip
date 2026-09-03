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
    """Fail-closed validator for V2 lifecycle/timeline invariants R001-R003."""

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

    return _result("preliminary-evidence-v2-foundation-r001-r003", violations)


def validate_m2_semantics(
    *,
    call: Mapping[str, Any],
    timeline: Mapping[str, Any],
    rtp_streams: Iterable[Mapping[str, Any]],
    findings: Iterable[Mapping[str, Any]] = (),
    clusters: Iterable[Mapping[str, Any]] = (),
    reported_problem_count: int | None = None,
    visibility: Mapping[str, Any] | None = None,
    claims: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate lifecycle + event/correlation/visibility rules implemented in M2.

    Covered rules: R001-R004, R007-R009, R014-R015. Remaining R005/R006/
    R010-R013 are added by later implementation phases; this function never
    marks those unimplemented rules as PASS.
    """

    rtp_list = [dict(stream) for stream in rtp_streams]
    finding_list = [dict(finding) for finding in findings]
    cluster_list = [dict(cluster) for cluster in clusters]
    base = validate_foundation_semantics(call=call, timeline=timeline, rtp_streams=rtp_list)
    violations = [SemanticViolation(**item) for item in base["violations"]]

    from .correlation import correlation_problem_count

    if reported_problem_count is not None:
        expected_count = correlation_problem_count(finding_list, cluster_list)
        if int(reported_problem_count) != expected_count:
            violations.append(
                SemanticViolation(
                    rule="R004",
                    severity="P0",
                    path="summary.problem_count",
                    detail=(
                        f"problem_count={reported_problem_count} but deterministic ABNORMAL/cluster "
                        f"count is {expected_count}; NORMAL/INFO/EXCLUSION-like evidence must not be counted."
                    ),
                )
            )

    for index, finding in enumerate(finding_list):
        events = [event for event in finding.get("events") or [] if isinstance(event, Mapping)]
        event_count = int(finding.get("event_count") or len(events))
        if event_count > 1 and finding.get("continuous") is True and not finding.get("continuous_evidence_refs"):
            violations.append(
                SemanticViolation(
                    rule="R007",
                    severity="P0",
                    path=f"findings[{index}].continuous",
                    detail="Multiple discrete events are rendered as a continuous anomaly without continuous evidence.",
                )
            )

        event_families = {
            str(event.get("event_family") or "").upper()
            for event in events
            if event.get("event_family")
        }
        finding_type = str(finding.get("type") or "").upper()
        if "LOSS" in finding_type and event_families and event_families <= {"TIMING"}:
            violations.append(
                SemanticViolation(
                    rule="R009",
                    severity="P0",
                    path=f"findings[{index}].type",
                    detail="Loss wording is used while the bound events contain timing evidence only.",
                )
            )

        if finding_type == "RTP_SEQUENCE_LOSS":
            metrics = finding.get("metrics") or {}
            if metrics.get("sequence_continuous") is True or int(metrics.get("lost_packets") or 0) == 0:
                violations.append(
                    SemanticViolation(
                        rule="R014",
                        severity="P0",
                        path=f"findings[{index}].metrics",
                        detail="RTP sequence loss claim contradicts continuous sequence / zero lost packet evidence.",
                    )
                )

    media_visibility = (visibility or {}).get("media") or {}
    if (claims or {}).get("end_to_end_media_complete") is True and media_visibility.get("end_to_end") != "COMPLETE":
        violations.append(
            SemanticViolation(
                rule="R008",
                severity="P0",
                path="claims.end_to_end_media_complete",
                detail="Report claims complete end-to-end media while visibility is not COMPLETE.",
            )
        )

    event_to_cluster: dict[str, str] = {}
    for cluster in cluster_list:
        cluster_id = str(cluster.get("cluster_id") or "")
        for member in cluster.get("member_events") or []:
            if isinstance(member, Mapping) and member.get("event_ref"):
                event_to_cluster[str(member["event_ref"])] = cluster_id

    for index, finding in enumerate(finding_list):
        if finding.get("independent_failure_domain") is True:
            continue
        cluster_ids = {
            event_to_cluster[str(ref)]
            for ref in finding.get("event_refs") or []
            if str(ref) in event_to_cluster
        }
        if len(cluster_ids) == 1:
            cluster_id = next(iter(cluster_ids))
            if finding.get("absorbed_by_cluster") != cluster_id:
                violations.append(
                    SemanticViolation(
                        rule="R015",
                        severity="P0",
                        path=f"findings[{index}].absorbed_by_cluster",
                        detail=f"Finding belongs to correlation cluster {cluster_id} but remains independently countable.",
                    )
                )

    return _result("preliminary-evidence-v2-m2-r001-r004-r007-r009-r014-r015", violations)


def _result(ruleset: str, violations: list[SemanticViolation]) -> dict[str, Any]:
    return {
        "status": "FAIL" if violations else "PASS",
        "ruleset": ruleset,
        "violations": [asdict(item) for item in violations],
    }

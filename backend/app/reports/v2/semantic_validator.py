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

    visibility_payload = visibility or {}
    media_visibility = visibility_payload.get("media") or {}
    end_to_end_visibility = visibility_payload.get("end_to_end_media") or media_visibility.get("end_to_end")
    if (claims or {}).get("end_to_end_media_complete") is True and end_to_end_visibility != "COMPLETE":
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


def validate_report_semantics(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete Preliminary Evidence Report V2 P0 ruleset.

    The validator consumes canonical report data only. It never asks an LLM to
    decide whether a rule passes. Any violation is fail-closed for user-visible
    COMPLETE projection.
    """

    call = report.get("call_reconstruction") or report.get("call") or {}
    timeline = report.get("timeline") or {}
    rtp_streams = report.get("rtp_streams") or []
    findings = [dict(item) for item in report.get("findings") or [] if isinstance(item, Mapping)]
    clusters = [dict(item) for item in report.get("correlation_clusters") or [] if isinstance(item, Mapping)]
    visibility = report.get("visibility") or {}
    claims = report.get("claims") or {}
    reported_problem_count = report.get("problem_count")
    if reported_problem_count is None:
        summary = report.get("summary") or {}
        if isinstance(summary, Mapping):
            reported_problem_count = summary.get("problem_count")

    base = validate_m2_semantics(
        call=call,
        timeline=timeline,
        rtp_streams=rtp_streams,
        findings=findings,
        clusters=clusters,
        reported_problem_count=reported_problem_count,
        visibility=visibility,
        claims=claims,
    )
    violations = [SemanticViolation(**item) for item in base["violations"]]

    finding_ids = {str(item.get("finding_id")) for item in findings if item.get("finding_id")}
    cluster_ids = {str(item.get("cluster_id")) for item in clusters if item.get("cluster_id")}
    abnormal_severities = {
        str(item.get("severity") or "").upper()
        for item in findings
        if str(item.get("class") or item.get("kind") or "ABNORMAL").upper() == "ABNORMAL"
        and item.get("severity")
    }

    for index, recommendation in enumerate(report.get("recommendations") or []):
        if not isinstance(recommendation, Mapping):
            continue
        missing_refs = [
            str(ref)
            for ref in recommendation.get("finding_refs") or []
            if str(ref) not in finding_ids
        ]
        missing_cluster_refs = [
            str(ref)
            for ref in recommendation.get("cluster_refs") or []
            if str(ref) not in cluster_ids
        ]
        referenced_severity = recommendation.get("target_severity") or recommendation.get("severity_ref")
        severity_missing = bool(
            referenced_severity
            and str(referenced_severity).upper() not in abnormal_severities
        )
        if missing_refs or missing_cluster_refs or severity_missing:
            violations.append(
                SemanticViolation(
                    rule="R005",
                    severity="P0",
                    path=f"recommendations[{index}]",
                    detail=(
                        "Recommendation references an absent finding/cluster/severity: "
                        f"finding_refs={missing_refs}, cluster_refs={missing_cluster_refs}, "
                        f"severity={referenced_severity if severity_missing else None}."
                    ),
                )
            )

    artifacts = [dict(item) for item in report.get("artifacts") or [] if isinstance(item, Mapping)]
    artifact_failures = [
        dict(item) for item in report.get("artifact_failures") or [] if isinstance(item, Mapping)
    ]
    for index, finding in enumerate(findings):
        finding_id = str(finding.get("finding_id") or "")
        requires_audio = bool(finding.get("requires_audio_clip")) or "AUDIO_CLIP" in {
            str(item).upper() for item in finding.get("artifact_requirements") or []
        }
        source_available = bool(finding.get("audio_source_available"))
        if not finding_id or not requires_audio or not source_available:
            continue
        bound = any(
            str(artifact.get("status") or "AVAILABLE").upper() == "AVAILABLE"
            and finding_id in {str(ref) for ref in artifact.get("finding_refs") or []}
            and str(artifact.get("artifact_requirement") or artifact.get("type") or "").upper()
            in {"AUDIO_CLIP", "ANOMALY_AUDIO_CLIP"}
            for artifact in artifacts
        )
        failed_structurally = any(
            str(failure.get("artifact_requirement") or "").upper() == "AUDIO_CLIP"
            and str(failure.get("status") or "").upper() == "FAILED"
            and failure.get("source_available") is True
            and failure.get("reason_code")
            and finding_id in {str(ref) for ref in failure.get("finding_refs") or []}
            for failure in artifact_failures
        )
        if not bound and not failed_structurally:
            violations.append(
                SemanticViolation(
                    rule="R006",
                    severity="P0",
                    path=f"findings[{index}].artifact_requirements",
                    detail="Audio source is available but no bound clip or structured render failure exists.",
                )
            )

    assessment = report.get("preliminary_assessment") or {}
    root_cause_status = None
    if isinstance(assessment, Mapping):
        root_cause_status = assessment.get("root_cause_status") or assessment.get("root_cause")
    ai_output = report.get("ai_output") or {}
    ai_confirmed = isinstance(ai_output, Mapping) and (
        ai_output.get("root_cause_confirmed") is True
        or str(ai_output.get("root_cause_status") or "").upper() == "CONFIRMED"
    )
    if str(root_cause_status or "").upper() == "CONFIRMED" or ai_confirmed:
        violations.append(
            SemanticViolation(
                rule="R010",
                severity="P0",
                path="preliminary_assessment.root_cause_status",
                detail="Preliminary/AI report is not authorized to confirm Root Cause independently.",
            )
        )

    for index, finding in enumerate(findings):
        finding_class = str(finding.get("class") or finding.get("kind") or "ABNORMAL").upper()
        if finding_class == "ABNORMAL" and not list(finding.get("evidence_refs") or []):
            violations.append(
                SemanticViolation(
                    rule="R011",
                    severity="P0",
                    path=f"findings[{index}].evidence_refs",
                    detail="ABNORMAL finding has no bound evidence reference.",
                )
            )

    provenance_required_fields = {
        "source_artifact_ids",
        "analyzer_name",
        "analyzer_version",
        "profile_version",
        "time_range",
        "sha256",
    }
    for index, artifact in enumerate(artifacts):
        if not (artifact.get("provenance_required") is True or artifact.get("critical") is True):
            continue
        missing = sorted(
            field
            for field in provenance_required_fields
            if artifact.get(field) in (None, "", [], {})
        )
        if missing:
            violations.append(
                SemanticViolation(
                    rule="R012",
                    severity="P0",
                    path=f"artifacts[{index}]",
                    detail=f"Critical artifact provenance is incomplete: missing {missing}.",
                )
            )

    anchors: dict[str, float] = {}
    if call.get("invite_time") is not None:
        anchors["invite"] = float(call["invite_time"])
    if call.get("established_time") is not None:
        anchors["established"] = float(call["established_time"])
    media_window = timeline.get("media_observation_window") or {}
    if isinstance(media_window, Mapping) and media_window.get("start") is not None:
        anchors["media_start"] = float(media_window["start"])

    events = [dict(item) for item in report.get("events") or [] if isinstance(item, Mapping)]
    if not events:
        for finding in findings:
            events.extend(dict(item) for item in finding.get("events") or [] if isinstance(item, Mapping))
    for index, event in enumerate(events):
        absolute = event.get("timestamp")
        if absolute is None:
            absolute = event.get("absolute_time")
        if absolute is None:
            continue
        absolute_value = float(absolute)
        for anchor_name, field in (
            ("invite", "relative_to_invite"),
            ("established", "relative_to_established"),
            ("media_start", "relative_to_media_start"),
        ):
            if field not in event or event.get(field) is None or anchor_name not in anchors:
                continue
            expected = absolute_value - anchors[anchor_name]
            if abs(float(event[field]) - expected) > 1e-6:
                violations.append(
                    SemanticViolation(
                        rule="R013",
                        severity="P0",
                        path=f"events[{index}].{field}",
                        detail=(
                            f"Relative event time {event[field]} disagrees with absolute time/"
                            f"{anchor_name} anchor; expected {expected:.6f}."
                        ),
                    )
                )

    return _result("preliminary-evidence-v2-r001-r015", violations)


def _result(ruleset: str, violations: list[SemanticViolation]) -> dict[str, Any]:
    return {
        "status": "FAIL" if violations else "PASS",
        "ruleset": ruleset,
        "violations": [asdict(item) for item in violations],
    }

from __future__ import annotations

from typing import Any, Iterable, Mapping


VALID_LEG_ROLES = {"CALLER", "CALLEE"}
VALID_DIRECTIONS = {"UPSTREAM", "DOWNSTREAM"}


def calculate_visibility(
    *,
    signaling_legs: Iterable[Mapping[str, Any]] = (),
    media_legs: Iterable[Mapping[str, Any]] = (),
    termination: Mapping[str, Any] | None = None,
    required_root_cause_evidence_complete: bool = False,
) -> dict[str, Any]:
    """Calculate scope visibility without collapsing it into pipeline status.

    Upstream/downstream are defined relative to the endpoint represented by the
    leg role. Role assignment itself belongs to deterministic call/SDP mapping;
    this module only evaluates the declared observations.
    """

    signaling = _signaling_visibility(signaling_legs)
    media = _media_visibility(media_legs)

    caller_media = media["caller"]
    callee_media = media["callee"]
    if caller_media == "BIDIRECTIONAL" and callee_media == "BIDIRECTIONAL":
        end_to_end = "COMPLETE"
    elif caller_media == "UNAVAILABLE" and callee_media == "UNAVAILABLE":
        end_to_end = "UNAVAILABLE"
    else:
        end_to_end = "PARTIAL"

    termination_observed = bool((termination or {}).get("observed"))
    root_cause_readiness = (
        "REVIEWABLE"
        if required_root_cause_evidence_complete and end_to_end == "COMPLETE"
        else "INSUFFICIENT"
    )

    return {
        "schema": "evidence-visibility-v2",
        "signaling": signaling,
        "media": media,
        "end_to_end_media": end_to_end,
        "termination": "OBSERVED" if termination_observed else "NOT_OBSERVED",
        "root_cause_readiness": root_cause_readiness,
    }


def _signaling_visibility(legs: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    role_events: dict[str, set[str]] = {"CALLER": set(), "CALLEE": set()}
    for leg in legs:
        role = str(leg.get("role") or "").upper()
        if role not in VALID_LEG_ROLES:
            continue
        for event in leg.get("observed") or []:
            role_events[role].add(str(event).upper())

    return {
        "caller": _signaling_state(role_events["CALLER"]),
        "callee": _signaling_state(role_events["CALLEE"]),
    }


def _signaling_state(observed: set[str]) -> str:
    if not observed:
        return "UNAVAILABLE"
    required = {"INVITE", "FINAL_RESPONSE", "ACK"}
    if required.issubset(observed):
        return "COMPLETE"
    return "PARTIAL"


def _media_visibility(legs: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    role_directions: dict[str, set[str]] = {"CALLER": set(), "CALLEE": set()}
    for leg in legs:
        role = str(leg.get("role") or "").upper()
        if role not in VALID_LEG_ROLES:
            continue
        for direction in leg.get("directions") or []:
            value = str(direction).upper()
            if value in VALID_DIRECTIONS:
                role_directions[role].add(value)

    return {
        "caller": _media_state(role_directions["CALLER"]),
        "callee": _media_state(role_directions["CALLEE"]),
    }


def _media_state(directions: set[str]) -> str:
    if directions == {"UPSTREAM", "DOWNSTREAM"}:
        return "BIDIRECTIONAL"
    if directions == {"UPSTREAM"}:
        return "UPSTREAM_ONLY"
    if directions == {"DOWNSTREAM"}:
        return "DOWNSTREAM_ONLY"
    return "UNAVAILABLE"

from __future__ import annotations

from typing import Any, Iterable, Mapping


VALID_LEG_ROLES = {"CALLER", "CALLEE"}
VALID_DIRECTIONS = {"UPSTREAM", "DOWNSTREAM"}
VALID_ACQUISITION = {"AVAILABLE", "PARTIAL", "MISSING"}


def calculate_visibility(
    *,
    acquisition: str = "AVAILABLE",
    signaling_legs: Iterable[Mapping[str, Any]] = (),
    media_legs: Iterable[Mapping[str, Any]] = (),
    termination: Mapping[str, Any] | None = None,
    required_root_cause_evidence_complete: bool = False,
) -> dict[str, Any]:
    """Calculate Evidence Visibility V2 without conflating pipeline status.

    The output follows SPEC §10. Direction detail remains explicit so a user
    facing renderer can say, for example, "主叫侧媒体双向可见" instead of
    over-claiming complete end-to-end media.

    ``end_to_end_media`` is the canonical report-level field. The nested
    ``media.end_to_end`` field is retained for early-V2 compatibility only.
    """

    acquisition_state = str(acquisition).upper()
    if acquisition_state not in VALID_ACQUISITION:
        raise ValueError(f"unsupported acquisition visibility: {acquisition}")

    signaling = _signaling_visibility(signaling_legs)
    media, media_directions = _media_visibility(media_legs)

    caller_media = media["caller_leg"]
    callee_media = media["callee_leg"]
    if caller_media == "BIDIRECTIONAL" and callee_media == "BIDIRECTIONAL":
        end_to_end = "COMPLETE"
    elif caller_media == "MISSING" and callee_media == "MISSING":
        end_to_end = "UNKNOWN"
    else:
        end_to_end = "PARTIAL"

    termination_observed = bool((termination or {}).get("observed"))
    root_cause_readiness = (
        "SUFFICIENT"
        if required_root_cause_evidence_complete and end_to_end == "COMPLETE"
        else "INSUFFICIENT"
    )

    return {
        "schema": "evidence-visibility-v2",
        "acquisition": acquisition_state,
        "signaling": signaling,
        "media": {
            **media,
            "end_to_end": end_to_end,
            "caller_leg_directions": media_directions["caller_leg"],
            "callee_leg_directions": media_directions["callee_leg"],
        },
        "end_to_end_media": end_to_end,
        "termination": "OBSERVED" if termination_observed else "NOT_OBSERVED",
        "root_cause_readiness": root_cause_readiness,
    }


def _signaling_visibility(legs: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    role_events: dict[str, set[str]] = {"CALLER": set(), "CALLEE": set()}
    role_seen = {"CALLER": False, "CALLEE": False}
    for leg in legs:
        role = str(leg.get("role") or "").upper()
        if role not in VALID_LEG_ROLES:
            continue
        role_seen[role] = True
        for event in leg.get("observed") or []:
            role_events[role].add(str(event).upper())

    return {
        "caller_leg": _signaling_state(role_events["CALLER"], role_seen["CALLER"]),
        "callee_leg": _signaling_state(role_events["CALLEE"], role_seen["CALLEE"]),
    }


def _signaling_state(observed: set[str], seen: bool) -> str:
    if not seen:
        return "MISSING"
    required = {"INVITE", "FINAL_RESPONSE", "ACK"}
    if required.issubset(observed):
        return "COMPLETE"
    return "PARTIAL"


def _media_visibility(
    legs: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    role_directions: dict[str, set[str]] = {"CALLER": set(), "CALLEE": set()}
    role_seen = {"CALLER": False, "CALLEE": False}
    for leg in legs:
        role = str(leg.get("role") or "").upper()
        if role not in VALID_LEG_ROLES:
            continue
        role_seen[role] = True
        for direction in leg.get("directions") or []:
            value = str(direction).upper()
            if value in VALID_DIRECTIONS:
                role_directions[role].add(value)

    states = {
        "caller_leg": _media_state(role_directions["CALLER"], role_seen["CALLER"]),
        "callee_leg": _media_state(role_directions["CALLEE"], role_seen["CALLEE"]),
    }
    direction_detail = {
        "caller_leg": sorted(role_directions["CALLER"]),
        "callee_leg": sorted(role_directions["CALLEE"]),
    }
    return states, direction_detail


def _media_state(directions: set[str], seen: bool) -> str:
    if not seen:
        return "MISSING"
    if directions == {"UPSTREAM", "DOWNSTREAM"}:
        return "BIDIRECTIONAL"
    if len(directions) == 1:
        return "ONE_WAY"
    return "PARTIAL"

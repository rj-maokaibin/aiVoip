from __future__ import annotations

import hashlib
import json
from typing import Any


CROSS_LAYER_SCHEMA_VERSION = "cross-layer-observation-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _observation_id(material: dict) -> str:
    return "xlay-" + hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()[:24]


def derive_first_observable_layer(layer_observations: list[dict]) -> dict:
    """Return a deterministic evidence boundary, never a physical root cause.

    Layer observations must be ordered along the media path. A layer may be
    declared first observable only when every earlier comparable layer is
    available and normal. Missing upstream/control evidence always yields
    UNKNOWN rather than a stronger inference.
    """
    if not layer_observations:
        return {"status": "UNKNOWN", "reason": "NO_LAYER_OBSERVATIONS"}
    for index, item in enumerate(layer_observations):
        layer = item.get("layer")
        if not item.get("available"):
            return {"status": "UNKNOWN", "reason": "UPSTREAM_EVIDENCE_MISSING", "missing_layer": layer}
        if item.get("abnormal"):
            previous = layer_observations[:index]
            if any(not x.get("available") for x in previous):
                return {"status": "UNKNOWN", "reason": "UPSTREAM_EVIDENCE_MISSING"}
            if any(x.get("abnormal") for x in previous):
                continue
            return {
                "status": "OBSERVED_BOUNDARY",
                "first_observable_layer": layer,
                "statement": f"异常首次可观测于 {layer}；这是证据边界，不等于异常物理起源或最终根因。",
            }
    return {"status": "NO_COMPARABLE_ANOMALY", "reason": "ALL_AVAILABLE_LAYERS_NORMAL"}


def build_cross_layer_observation(*, observation_type: str, call_id: str | None,
                                  time_range: dict | None, layers: list[dict],
                                  source_refs: list[dict] | None = None,
                                  context: dict | None = None) -> dict:
    normalized_layers = []
    for item in layers:
        normalized_layers.append({
            "layer": item.get("layer"),
            "available": bool(item.get("available")),
            "abnormal": bool(item.get("abnormal")) if item.get("available") else False,
            "role": item.get("role"),
            "source_ref": item.get("source_ref"),
            "metrics": dict(item.get("metrics") or {}),
        })
    boundary = derive_first_observable_layer(normalized_layers)
    material = {
        "type": observation_type,
        "call_id": call_id,
        "time_range": time_range or {},
        "layers": normalized_layers,
    }
    return {
        "schema_version": CROSS_LAYER_SCHEMA_VERSION,
        "observation_id": _observation_id(material),
        "type": observation_type,
        "call_id": call_id,
        "time_range": dict(time_range or {}),
        "layers": normalized_layers,
        "first_observable_boundary": boundary,
        "source_refs": list(source_refs or []),
        "context": dict(context or {}),
        "root_cause_boundary": (
            "Cross-Layer Observation 仅描述当前证据链中的层间关系和首次可观测边界；"
            "不得据此直接确认电源、接地、SLIC、网络设备或其他物理根因。"
        ),
    }


def silence_candidate_observation(candidate: dict) -> dict | None:
    """Build a cross-layer boundary only for an accepted silence candidate.

    The correlated RTP track is intentionally named CORRELATED_RTP_INPUT rather
    than DUT_DOWNSTREAM unless DUT endpoint role is independently established.
    This avoids inventing device direction from a stream correlation alone.
    """
    if str(candidate.get("type") or "") != "UNEXPECTED_SILENCE":
        return None
    if str(candidate.get("decision") or "") != "ACCEPT":
        return None
    scope = candidate.get("scope") or {}
    context = candidate.get("context") or {}
    activity = context.get("counterpart_rtp_activity") or {}
    rtp_available = bool(context.get("counterpart_rtp_stream_id")) and activity.get("status") in {"ACTIVE", "LOW_ENERGY", "AMBIGUOUS"}
    rtp_abnormal = activity.get("status") in {"LOW_ENERGY"}
    pcm_layer = str(scope.get("pcm_tap") or "PCM").upper()
    layers = [
        {
            "layer": "CORRELATED_RTP_INPUT",
            "available": rtp_available,
            "abnormal": rtp_abnormal,
            "role": "CONTROL_OR_UPSTREAM_MEDIA",
            "source_ref": context.get("counterpart_rtp_stream_id"),
            "metrics": activity,
        },
        {
            "layer": pcm_layer,
            "available": True,
            "abnormal": True,
            "role": "PCM_SILENCE_CANDIDATE",
            "source_ref": {
                "pcm_tap": scope.get("pcm_tap"),
                "pcm_session_index": scope.get("pcm_session_index"),
            },
            "metrics": candidate.get("metrics") or {},
        },
    ]
    return build_cross_layer_observation(
        observation_type="UNEXPECTED_SILENCE_PATH",
        call_id=scope.get("call_id"),
        time_range=candidate.get("time_range") or {},
        layers=layers,
        source_refs=[
            {"type": "DIAGNOSTIC_CANDIDATE", "id": candidate.get("candidate_id")},
            {"type": "RTP_STREAM", "id": context.get("counterpart_rtp_stream_id")},
        ],
        context={"candidate_reason_codes": list(candidate.get("reason_codes") or [])},
    )


def _periodic_level(node: dict | None) -> tuple[bool, bool, dict]:
    if node is None:
        return False, False, {}
    level = str(node.get("level") or "LOW").upper()
    return True, level in {"MEDIUM", "HIGH"}, {"level": level, "strength": node.get("strength"), "representative": node.get("representative")}


def periodic_finding_observation(finding: dict) -> dict | None:
    if str(finding.get("type") or "") not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE", "PERIODIC_INTERFERENCE_PATH_COMPARISON"}:
        return None
    metrics = finding.get("metrics") or {}
    down_available, down_abnormal, down_metrics = _periodic_level(metrics.get("downstream_rtp"))
    pcm_available, pcm_abnormal, pcm_metrics = _periodic_level(metrics.get("pcm_rx"))
    up_available, up_abnormal, up_metrics = _periodic_level(metrics.get("upstream_rtp"))
    return build_cross_layer_observation(
        observation_type="PERIODIC_INTERFERENCE_PATH",
        call_id=(finding.get("scope") or {}).get("call_id"),
        time_range=finding.get("time_range") or {},
        layers=[
            {"layer": "RTP_DOWNSTREAM", "available": down_available, "abnormal": down_abnormal, "role": "CONTROL", "metrics": down_metrics},
            {"layer": "PCM_RX", "available": pcm_available, "abnormal": pcm_abnormal, "role": "LOCAL_CAPTURE_TAP", "metrics": pcm_metrics},
            {"layer": "RTP_UPSTREAM", "available": up_available, "abnormal": up_abnormal, "role": "ENCODED_UPLINK", "metrics": up_metrics},
        ],
        source_refs=list(finding.get("evidence_refs") or []),
        context={"finding_type": finding.get("type")},
    )

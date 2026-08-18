from __future__ import annotations

from app.analyzers.profile import get_default_analyzer_profile


def dtmf_quality_events(events: list[dict]) -> list[dict]:
    """Derive deterministic DTMF quality anomalies from accepted in-band events.

    This does not infer a missing/incorrect dialed digit because the detector has no
    authoritative user-intent source. It only reports measurable signal-quality and
    inter-digit-timing abnormalities using versioned Analyzer Profile thresholds.
    """
    if not events:
        return []
    cfg = get_default_analyzer_profile().section("dtmf")
    min_conf = float(cfg.get("quality_min_confidence", 0.55))
    min_gap_ms = float(cfg.get("min_interdigit_gap_ms", 40.0))
    out: list[dict] = []
    for index, event in enumerate(events):
        confidence = float(event.get("confidence") or 0.0)
        if confidence < min_conf:
            out.append({
                "type": "DTMF_LOW_CONFIDENCE",
                "severity": "MEDIUM",
                "event_index": index,
                "digit": event.get("digit"),
                "start_seconds": event.get("start_seconds"),
                "end_seconds": event.get("end_seconds"),
                "duration_ms": event.get("duration_ms"),
                "confidence": confidence,
                "threshold": min_conf,
                "reason": "detected DTMF candidate confidence is below the versioned quality threshold",
            })
    for index, (previous, current) in enumerate(zip(events, events[1:]), start=1):
        try:
            gap_ms = (float(current["start_seconds"]) - float(previous["end_seconds"])) * 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if gap_ms < min_gap_ms:
            out.append({
                "type": "DTMF_SHORT_INTERDIGIT_GAP",
                "severity": "MEDIUM",
                "previous_event_index": index - 1,
                "event_index": index,
                "previous_digit": previous.get("digit"),
                "digit": current.get("digit"),
                "start_seconds": current.get("start_seconds"),
                "end_seconds": current.get("end_seconds"),
                "gap_ms": round(gap_ms, 3),
                "threshold_ms": min_gap_ms,
                "reason": "inter-digit gap is below the versioned quality threshold",
            })
    return sorted(out, key=lambda x: (float(x.get("start_seconds") or 0.0), str(x.get("type"))))

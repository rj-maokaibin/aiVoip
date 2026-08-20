from __future__ import annotations


CANDIDATE_AUDIO_CLIP = "CANDIDATE_AUDIO_CLIP"
PROMOTED_AUDIO_CLIP = "AUDIO_CLIP"


def _candidate_kind(value: str | None) -> str | None:
    raw = str(value or "").upper()
    if raw == "CLICK_POP":
        return "CLICK_POP"
    if raw in {"SILENCE", "UNEXPECTED_SILENCE"}:
        return "UNEXPECTED_SILENCE"
    return None


def sanitize_gated_media_pcm(media: dict) -> dict:
    """Move raw PCM detector events behind the candidate boundary.

    CandidateDecision must run before this function. The deterministic Diagnosis
    reasoner historically reads ``result.pcm.*.click_pop_events/silence_events`` as
    a fallback. Clearing those fields after preserving them under ``*_candidates``
    prevents a rejected candidate from bypassing the Media cross-layer gate.
    """
    pcm = media.get("pcm") or {}
    click_count = 0
    silence_count = 0
    for stream in pcm.get("streams", []) or []:
        for session in stream.get("sessions", []) or []:
            clicks = list(session.get("click_pop_events") or [])
            silences = list(session.get("silence_events") or [])
            if clicks:
                session["click_pop_candidates"] = clicks
                click_count += len(clicks)
            else:
                session.setdefault("click_pop_candidates", [])
            if silences:
                session["silence_candidates"] = silences
                silence_count += len(silences)
            else:
                session.setdefault("silence_candidates", [])
            session["click_pop_events"] = []
            session["silence_events"] = []
    media.setdefault("summary", {})["raw_pcm_candidates"] = {
        "click_pop_candidate_count": click_count,
        "silence_candidate_count": silence_count,
        "downstream_visibility": "CANDIDATE_ONLY",
    }
    return media


def gate_candidate_audio_artifacts(media: dict, *, tolerance_seconds: float = 0.25) -> dict:
    """Keep rejected/raw detector clips out of the main anomaly-audio surface.

    PCM detector clips are generated before CandidateDecision. They are retained for
    audit as CANDIDATE_AUDIO_CLIP. A clip is promoted back to AUDIO_CLIP only when a
    PROMOTED CandidateDecision for the same tap/session/type is close in time.
    """
    decisions = [d for d in media.get("candidate_decisions", []) or [] if d.get("status") == "PROMOTED"]
    promoted = []
    for decision in decisions:
        scope = decision.get("scope") or {}
        promoted.append({
            "candidate_id": decision.get("candidate_id"),
            "kind": _candidate_kind(decision.get("candidate_type")),
            "time": float(decision.get("candidate_time") or 0.0),
            "pcm_tap": scope.get("pcm_tap"),
            "session_index": scope.get("pcm_session_index"),
            "reason_code": decision.get("reason_code"),
        })

    promoted_count = 0
    quarantined_count = 0
    for artifact in media.get("artifacts", []) or []:
        meta = artifact.get("metadata") or {}
        kind = _candidate_kind(meta.get("event_type"))
        if artifact.get("type") != "AUDIO_CLIP" or kind is None or not meta.get("pcm_tap"):
            continue
        # Raw PCM anomaly clips must prove a promoted decision before being exposed
        # as a main AUDIO_CLIP attachment.
        artifact["type"] = CANDIDATE_AUDIO_CLIP
        meta["candidate_artifact_status"] = "RAW_UNDECIDED"
        quarantined_count += 1
        event_time = float(meta.get("event_time") or 0.0)
        matches = [p for p in promoted if p["kind"] == kind
                   and p["pcm_tap"] == meta.get("pcm_tap")
                   and p["session_index"] == meta.get("session_index")]
        if not matches:
            artifact["metadata"] = meta
            continue
        best = min(matches, key=lambda p: abs(p["time"] - event_time))
        delta = abs(best["time"] - event_time)
        if delta <= tolerance_seconds:
            artifact["type"] = PROMOTED_AUDIO_CLIP
            meta["candidate_artifact_status"] = "PROMOTED"
            meta["candidate_id"] = best["candidate_id"]
            meta["candidate_reason_code"] = best["reason_code"]
            meta["candidate_time_delta_ms"] = round(delta * 1000.0, 3)
            promoted_count += 1
        artifact["metadata"] = meta

    summary = media.setdefault("summary", {})
    summary["candidate_audio_artifacts"] = {
        "raw_candidate_clip_count": quarantined_count,
        "promoted_audio_clip_count": promoted_count,
        "quarantined_candidate_clip_count": quarantined_count - promoted_count,
    }
    return media

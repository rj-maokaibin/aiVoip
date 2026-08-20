from __future__ import annotations

from collections import Counter

from app.contracts.diagnostic import CandidateDecisionStatus, build_candidate_decision, build_diagnostic_event


UNRESOLVED_PCM_RULE_VERSION = "pcm-unresolved-candidate-adapter-v1"


def _state_meta(states: dict[str, dict]) -> dict:
    state = states.get("pcm_intelligence") or {}
    return {
        "analyzer_version": state.get("analyzer_version") or state.get("version"),
        "profile_version": state.get("config_version"),
        "profile_checksum": state.get("config_checksum"),
    }


def _add(snapshot: dict, event: dict, decision: dict) -> None:
    events = {str(x["event_id"]): x for x in snapshot.get("events", []) or []}
    decisions = {str(x["decision_id"]): x for x in snapshot.get("candidate_decisions", []) or []}
    events[str(event["event_id"])] = event
    decisions[str(decision["decision_id"])] = decision
    snapshot["events"] = sorted(events.values(), key=lambda x: x["event_id"])
    snapshot["candidate_decisions"] = sorted(decisions.values(), key=lambda x: x["decision_id"])
    counts = Counter(str(x["status"]) for x in decisions.values())
    summary = dict(snapshot.get("summary") or {})
    summary["event_count"] = len(events)
    summary["candidate_decision_count"] = len(decisions)
    summary["decision_status_counts"] = dict(sorted(counts.items()))
    summary.setdefault("finding_fallback_event_count", 0)
    summary.setdefault("finding_merge_decision_count", 0)
    snapshot["summary"] = summary


def append_unresolved_pcm_candidates(
    snapshot: dict,
    *,
    results: dict[str, dict | None],
    analyzer_states: dict[str, dict],
) -> dict:
    """Retain raw PCM candidates when Media gating cannot run.

    `apply_candidate_decisions` already moves raw detector hits into
    `click_pop_candidates` / `silence_candidates` when Media Analyzer is absent.
    PR7 represents those facts as INCONCLUSIVE Events rather than silently
    dropping them from the canonical diagnostic audit trail.
    """
    if isinstance(results.get("media_intelligence"), dict):
        return snapshot
    pcm = results.get("pcm_intelligence")
    if not isinstance(pcm, dict):
        return snapshot

    meta = _state_meta(analyzer_states)
    for stream_index, stream in enumerate(pcm.get("streams", []) or []):
        tap = stream.get("tap") or {}
        tap_name = str(tap.get("name") or "pcm")
        direction = tap.get("direction")
        for session_pos, session in enumerate(stream.get("sessions", []) or []):
            session_start = float(session.get("start_time") or 0.0)
            session_index = session.get("session_index")
            scope = {
                "layer": tap_name.upper(),
                "pcm_tap": tap_name,
                "pcm_direction": direction,
                "pcm_session_index": session_index,
            }
            for index, raw in enumerate(session.get("click_pop_candidates", []) or []):
                when = session_start + float(raw.get("time_seconds") or 0.0)
                event = build_diagnostic_event(
                    event_type="CLICK_POP",
                    analyzer_id="pcm_intelligence",
                    scope=scope,
                    time_range={"start": when, "end": when, "representative": when},
                    measurements=raw,
                    context={"candidate_stage": "RAW_PCM", "media_gate_available": False},
                    source_ref={
                        "source": "pcm.click_pop_candidates",
                        "stream_index": stream_index,
                        "session_index": session_pos,
                        "index": index,
                    },
                    **meta,
                )
                decision = build_candidate_decision(
                    event,
                    status=CandidateDecisionStatus.INCONCLUSIVE,
                    reason_code="MEDIA_ANALYZER_UNAVAILABLE",
                    reason="PCM detector 发现候选，但 Media/跨层负控不可用；保留审计，不生成用户 Finding。",
                    rule_version=UNRESOLVED_PCM_RULE_VERSION,
                )
                _add(snapshot, event, decision)

            for index, raw in enumerate(session.get("silence_candidates", []) or []):
                start = session_start + float(raw.get("start_seconds") or 0.0)
                end = session_start + float(raw.get("end_seconds") if raw.get("end_seconds") is not None else raw.get("start_seconds") or 0.0)
                event = build_diagnostic_event(
                    event_type="UNEXPECTED_SILENCE",
                    analyzer_id="pcm_intelligence",
                    scope=scope,
                    time_range={"start": start, "end": end, "representative": start},
                    measurements=raw,
                    context={"candidate_stage": "RAW_PCM", "media_gate_available": False},
                    source_ref={
                        "source": "pcm.silence_candidates",
                        "stream_index": stream_index,
                        "session_index": session_pos,
                        "index": index,
                    },
                    **meta,
                )
                decision = build_candidate_decision(
                    event,
                    status=CandidateDecisionStatus.INCONCLUSIVE,
                    reason_code="MEDIA_ANALYZER_UNAVAILABLE",
                    reason="PCM detector 发现静音候选，但方向 RTP/Media 负控不可用；保留审计，不生成用户 Finding。",
                    rule_version=UNRESOLVED_PCM_RULE_VERSION,
                )
                _add(snapshot, event, decision)
    return snapshot

from app.analyzers.media.candidate_artifacts import gate_candidate_audio_artifacts


def _clip(kind: str, when: float, *, tap: str = "pcm_rx", session: int = 0) -> dict:
    return {
        "type": "AUDIO_CLIP",
        "filename": f"{kind}-{when}.wav",
        "content_type": "audio/wav",
        "metadata": {
            "pcm_tap": tap,
            "session_index": session,
            "event_type": kind,
            "event_time": when,
        },
    }


def test_rejected_candidate_clip_is_quarantined_from_main_audio_surface():
    media = {
        "summary": {},
        "candidate_decisions": [{
            "status": "REJECTED_NEGATIVE_CONTROL",
            "candidate_id": "candidate-dtmf",
            "candidate_type": "CLICK_POP",
            "candidate_time": 114.332755,
            "reason_code": "DTMF_OVERLAP",
            "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 0},
        }],
        "artifacts": [_clip("CLICK_POP", 114.332755)],
    }

    gate_candidate_audio_artifacts(media)

    artifact = media["artifacts"][0]
    assert artifact["type"] == "CANDIDATE_AUDIO_CLIP"
    assert artifact["metadata"]["candidate_artifact_status"] == "RAW_UNDECIDED"
    assert media["summary"]["candidate_audio_artifacts"]["promoted_audio_clip_count"] == 0


def test_promoted_candidate_clip_is_exposed_as_audio_clip_with_decision_provenance():
    media = {
        "summary": {},
        "candidate_decisions": [{
            "status": "PROMOTED",
            "candidate_id": "candidate-real-click",
            "candidate_type": "CLICK_POP",
            "candidate_time": 130.0,
            "reason_code": "ACTIVE_MEDIA_MULTI_FEATURE_CLICK",
            "scope": {"pcm_tap": "pcm_rx", "pcm_session_index": 0},
        }],
        "artifacts": [_clip("CLICK_POP", 130.04)],
    }

    gate_candidate_audio_artifacts(media)

    artifact = media["artifacts"][0]
    assert artifact["type"] == "AUDIO_CLIP"
    assert artifact["metadata"]["candidate_artifact_status"] == "PROMOTED"
    assert artifact["metadata"]["candidate_id"] == "candidate-real-click"
    assert artifact["metadata"]["candidate_time_delta_ms"] == 40.0


def test_unrelated_rtp_packet_loss_audio_clip_is_not_reclassified():
    artifact = {
        "type": "AUDIO_CLIP",
        "filename": "rtp-loss.wav",
        "content_type": "audio/wav",
        "metadata": {"stream_id": "rtp-up", "event_type": "PACKET_LOSS", "event_time": 140.0},
    }
    media = {"summary": {}, "candidate_decisions": [], "artifacts": [artifact]}

    gate_candidate_audio_artifacts(media)

    assert artifact["type"] == "AUDIO_CLIP"

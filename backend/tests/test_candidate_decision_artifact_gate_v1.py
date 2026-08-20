from app.services.evidence_report_source_artifacts import report_source_artifact_allowed


def test_raw_click_clip_without_candidate_decision_is_not_report_source():
    assert report_source_artifact_allowed(
        artifact_type="AUDIO_CLIP",
        metadata={"event_type": "CLICK_POP", "pcm_tap": "pcm_rx"},
    ) is False


def test_suppressed_silence_clip_is_not_report_source():
    assert report_source_artifact_allowed(
        artifact_type="AUDIO_CLIP",
        metadata={"event_type": "SILENCE", "candidate_decision_status": "SUPPRESSED"},
    ) is False


def test_inconclusive_silence_clip_is_not_report_source():
    assert report_source_artifact_allowed(
        artifact_type="AUDIO_CLIP",
        metadata={"event_type": "UNEXPECTED_SILENCE", "candidate_decision_status": "INCONCLUSIVE"},
    ) is False


def test_promoted_click_clip_can_enter_report_source_set():
    assert report_source_artifact_allowed(
        artifact_type="AUDIO_CLIP",
        metadata={"event_type": "CLICK_POP", "candidate_decision_status": "PROMOTED"},
    ) is True


def test_rtp_high_delta_clip_is_not_affected_by_candidate_gate():
    assert report_source_artifact_allowed(
        artifact_type="AUDIO_CLIP",
        metadata={"event_type": "HIGH_DELTA", "stream_id": "rtp-up"},
    ) is True


def test_periodic_interference_clip_is_not_affected_by_candidate_gate():
    assert report_source_artifact_allowed(
        artifact_type="PERIODIC_AUDIO_CLIP",
        metadata={"event_type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE"},
    ) is True

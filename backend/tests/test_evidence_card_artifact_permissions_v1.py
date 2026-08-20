from __future__ import annotations

from app.api.v1.artifacts import REPORT_SAFE_TYPES


def test_evidence_card_promoted_clips_are_report_safe_but_raw_and_candidates_are_not():
    assert "AUDIO_CLIP" in REPORT_SAFE_TYPES
    assert "PERIODIC_AUDIO_CLIP" in REPORT_SAFE_TYPES
    assert "CANDIDATE_AUDIO_CLIP" not in REPORT_SAFE_TYPES
    assert "PCM_WAV" not in REPORT_SAFE_TYPES
    assert "RTP_WAV" not in REPORT_SAFE_TYPES
    assert "AUDIO_WAV" not in REPORT_SAFE_TYPES
    assert "RAW_PCAP" not in REPORT_SAFE_TYPES

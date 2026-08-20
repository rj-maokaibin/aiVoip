from __future__ import annotations

from app.api.v1.artifacts import REPORT_SAFE_TYPES
from app.reports.evidence_card import build_evidence_card


def test_evidence_card_promoted_clips_are_report_safe_but_raw_and_candidates_are_not():
    assert "AUDIO_CLIP" in REPORT_SAFE_TYPES
    assert "PERIODIC_AUDIO_CLIP" in REPORT_SAFE_TYPES
    assert "CANDIDATE_AUDIO_CLIP" not in REPORT_SAFE_TYPES
    assert "PCM_WAV" not in REPORT_SAFE_TYPES
    assert "RTP_WAV" not in REPORT_SAFE_TYPES
    assert "AUDIO_WAV" not in REPORT_SAFE_TYPES
    assert "RAW_PCAP" not in REPORT_SAFE_TYPES


def test_audio_wav_mime_cannot_override_raw_artifact_type_boundary():
    finding={
        "finding_id":"f1","type":"HIGH_DELTA","severity":"MEDIUM","title":"delta","observation":"delta",
        "root_cause_boundary":"preliminary","time_range":{"start":1.0,"end":1.0,"representative":1.0},
        "scope":{"layer":"RTP","rtp_stream_id":"up"},"metrics":{},
        "artifact_refs":[{"artifact_id":"raw1","type":"AUDIO_WAV","filename":"full.wav","content_type":"audio/wav","metadata":{"stream_id":"up"}}],
    }
    card=build_evidence_card(finding)
    assert card["audio_evidence"]["status"]=="UNAVAILABLE"
    assert card["audio_evidence"]["clips"]==[]
    assert card["detail_artifacts"][0]["content_url"] is None


def test_periodic_clip_string_source_is_safe_and_preserved_for_golden_traceability():
    finding={
        "finding_id":"f2","type":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","severity":"HIGH","title":"periodic","observation":"periodic",
        "root_cause_boundary":"preliminary","time_range":{"start":1.0,"end":3.0,"representative":2.0},
        "scope":{"layer":"pcm_rx","pcm_tap":"pcm_rx","pcm_session_index":0,"upstream_rtp_stream_id":"up"},"metrics":{},
        "artifact_refs":[{
            "artifact_id":"clip1","type":"PERIODIC_AUDIO_CLIP","filename":"periodic_rtp_up.wav","content_type":"audio/wav",
            "metadata":{"event_type":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","source":"rtp_up","scope":{"pcm_tap":"pcm_rx","pcm_session_index":0,"upstream_rtp_stream_id":"up"}},
        }],
    }
    card=build_evidence_card(finding)
    clip=card["audio_evidence"]["clips"][0]
    assert card["audio_evidence"]["status"]=="AVAILABLE"
    assert clip["source"]=="rtp_up"
    assert clip["direction"] is None
    assert clip["content_url"]=="/api/v1/artifacts/clip1/content"

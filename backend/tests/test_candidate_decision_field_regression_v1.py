from copy import deepcopy

from app.analyzers.media.candidate_decision import REJECTED_NEGATIVE_CONTROL, apply_candidate_decisions
from app.reports.finding_composer import compose_findings


def test_field_like_dtmf_onset_click_9ms_later_is_not_report_finding():
    # Mirrors the reviewed field capture relationship: DTMF "6" begins at
    # ~4.323755 s in pcm_rx and the raw Click candidate occurs ~9 ms later.
    session_start = 1786690960.0
    dtmf_start_rel = 4.323755
    click_rel = 4.332755
    media_start = 1786690972.052840
    media_end = 1786691020.535864

    embedded_pcm = {
        "summary": {},
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{
                "session_index": 0,
                "start_time": session_start,
                "end_time": 1786691025.0,
                "dtmf_events": [{
                    "digit": "6",
                    "start_seconds": dtmf_start_rel,
                    "end_seconds": dtmf_start_rel + 0.14,
                    "duration_ms": 140.0,
                    "confidence": 0.99,
                }],
                "click_pop_events": [{"time_seconds": click_rel, "confidence": 0.94, "jump": 12000}],
                "silence_events": [],
            }],
        }],
    }
    media = {
        "summary": {},
        "packet": {"calls": [{
            "call_id": "00ad1c804c33b255@192.168.3.200",
            "media_start_time": media_start,
            "media_end_time": media_end,
        }]},
        "pcm": embedded_pcm,
        "rtp_audio_tracks": [],
        "correlations": [],
        # The field-like click is pre-call, so no Active Media candidate should own it.
        "active_media_audio_events": [],
        "cross_layer_events": [],
    }
    standalone_pcm = deepcopy(embedded_pcm)
    results = {
        "packet_intelligence": None,
        "pcm_intelligence": standalone_pcm,
        "media_intelligence": media,
    }

    normalized = apply_candidate_decisions(results)
    decisions = normalized["media_intelligence"]["candidate_decisions"]
    findings = compose_findings(
        packet=None,
        pcm=normalized["pcm_intelligence"],
        media=normalized["media_intelligence"],
        source_run_ids={"pcm_intelligence": "pcm-run", "media_intelligence": "media-run"},
    )

    assert len(decisions) == 1
    assert decisions[0]["status"] == REJECTED_NEGATIVE_CONTROL
    assert decisions[0]["reason_code"] == "DTMF_OVERLAP"
    assert decisions[0]["negative_controls"][0]["digit"] == "6"
    assert not any(f["type"] == "CLICK_POP" for f in findings)


def test_active_media_raw_candidate_is_not_double_counted_when_media_candidate_owns_decision():
    session_start = 100.0
    embedded_pcm = {
        "summary": {},
        "streams": [{
            "tap": {"name": "pcm_rx", "direction": "RX"},
            "sessions": [{
                "session_index": 0,
                "start_time": session_start,
                "end_time": 160.0,
                "dtmf_events": [],
                "click_pop_events": [{"time_seconds": 30.0, "confidence": 0.95, "jump": 10000}],
                "silence_events": [],
            }],
        }],
    }
    active_event = {
        "type": "CLICK_POP",
        "time": 130.0,
        "severity": "MEDIUM",
        "evidence_level": "L3",
        "scope": {
            "call_id": "sip-call",
            "pcm_tap": "pcm_rx",
            "pcm_session_index": 0,
            "active_media_window": {"start_time": 110.0, "end_time": 150.0},
        },
        "details": {"confidence": 0.95},
    }
    media = {
        "summary": {},
        "packet": {"calls": [{"call_id": "sip-call", "media_start_time": 110.0, "media_end_time": 150.0}]},
        "pcm": embedded_pcm,
        "rtp_audio_tracks": [],
        "correlations": [],
        "active_media_audio_events": [active_event],
        "cross_layer_events": [active_event],
    }

    normalized = apply_candidate_decisions({
        "packet_intelligence": None,
        "pcm_intelligence": deepcopy(embedded_pcm),
        "media_intelligence": media,
    })

    decisions = normalized["media_intelligence"]["candidate_decisions"]
    assert len(decisions) == 1
    assert decisions[0]["status"] == "PROMOTED"
    assert normalized["media_intelligence"]["summary"]["candidate_decision"]["total"] == 1

from __future__ import annotations

from app.reports.evidence_card import attach_evidence_cards, build_evidence_card
from app.reports.evidence_brief import render_report_html


def _high_delta_finding(*, with_audio: bool = True) -> dict:
    refs=[
        {
            "artifact_id":"img-1","type":"RTP_TIMELINE_PNG","filename":"finding_rtp.png","content_type":"image/png","role":"FINDING",
            "metadata":{"annotation_complete":True,"annotation_contract":{"title":"RTP HIGH_DELTA","caption":"异常点附近 RTP Timeline"},"source":{"stream_id":"rtp-up"}},
        }
    ]
    if with_audio:
        refs.append({
            "artifact_id":"audio-1","type":"AUDIO_CLIP","filename":"rtp-high-delta.wav","content_type":"audio/wav","role":"FINDING",
            "metadata":{"event_type":"HIGH_DELTA","event_time":110.146,"stream_id":"rtp-up"},
        })
    return {
        "finding_id":"finding-1","stable_key":"stable-1","type":"HIGH_DELTA","severity":"MEDIUM","evidence_level":"L2",
        "title":"RTP 包间隔异常增大（HIGH_DELTA）","observation":"DUT 上行 RTP 共观测到 2 次包间隔异常。",
        "interpretation":"Sequence 连续，因此属于 Delay/Stall，不应写成 Packet Loss。",
        "root_cause_boundary":"不能单凭 HIGH_DELTA 区分 DUT 调度、网络排队或抓包观察点。",
        "time_range":{"start":110.146,"end":110.175,"representative":110.146},
        "scope":{"layer":"RTP","call_id":"CALL-001","rtp_stream_id":"rtp-up","direction":"192.168.150.4:10000->192.168.3.200:11446","ssrc":77},
        "metrics":{
            "event_count":2,"max_delta_ms":175.043,"ptime_ms":20.0,"max_excess_delay_ms":155.043,
            "stream_lost_packets":0,"all_sequence_continuous":True,
            "events":[
                {"time":110.146,"previous_frame_number":20272,"current_frame_number":20285,"previous_sequence":46511,"current_sequence":46512,"delta_ms":146.083,"classification":"INTERARRIVAL_STALL_WITHOUT_RTP_GAP"},
                {"time":110.175,"previous_frame_number":20329,"current_frame_number":20344,"previous_sequence":46519,"current_sequence":46520,"delta_ms":175.043,"classification":"INTERARRIVAL_STALL_WITHOUT_RTP_GAP"},
            ],
        },
        "artifact_refs":refs,"evidence_refs":[{"type":"PCAP","id":"pcap-1"}],"event_refs":[{"source":"packet.anomalies","index":1}],"source_analyzer_run_ids":["run-packet"],
    }


def test_high_delta_evidence_card_has_time_scope_measurements_frame_seq_visual_audio_and_next_action():
    card=build_evidence_card(_high_delta_finding(),call={"started_at":100.0})

    assert card["version"]=="evidence-card-v1"
    assert card["time"]["absolute_start_utc"].startswith("1970-01-01T00:01:50.146")
    assert card["time"]["call_relative_representative"]=="T+10.146s"
    assert card["scope"]["direction"]=="192.168.150.4:10000->192.168.3.200:11446"
    assert any(x["label"]=="最大 RTP 包间隔" and x["value"]==175.043 and x["unit"]=="ms" for x in card["measurements"])
    assert card["packet_refs"][0]["previous_frame"]==20272
    assert card["packet_refs"][1]["current_seq"]==46520
    assert card["visual_evidence"][0]["content_url"]=="/api/v1/artifacts/img-1/content"
    assert card["visual_evidence"][0]["annotation_complete"] is True
    assert card["audio_evidence"]["status"]=="AVAILABLE"
    assert card["audio_evidence"]["clips"][0]["content_url"]=="/api/v1/artifacts/audio-1/content"
    assert "PCM RX/TX" in card["next_action"]
    assert "不能单凭 HIGH_DELTA" in card["root_cause_boundary"]


def test_audio_expected_finding_without_matching_clip_is_explicitly_unavailable_not_silently_empty():
    card=build_evidence_card(_high_delta_finding(with_audio=False),call={"started_at":100.0})

    assert card["audio_evidence"]["status"]=="UNAVAILABLE"
    assert "NO_MATCHING_ANOMALY_AUDIO_CLIP" in card["audio_evidence"]["reason"]
    assert "不得用其他时间窗" in card["audio_evidence"]["reason"]


def test_html_renders_key_visual_audio_tplus_and_packet_drilldown_inside_finding_card():
    finding=_high_delta_finding()
    payload={
        "schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v2","report_version":1,"generated_at":"2026-08-20T00:00:00Z",
        "case":{"case_no":"CASE-1","summary":"noise"},"scope":{"type":"CASE","id":"CASE-1"},"headline":"发现 HIGH_DELTA",
        "finding_count":1,"highest_severity":"MEDIUM","findings":[finding],"completeness":{"state":"COMPLETE","reviewability":"FULLY_REVIEWABLE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True},"boundary":"complete"},
        "evidence_boundary":{"statement":"不确认最终根因。"},"preliminary_assessment":{"summary":"发现 HIGH_DELTA"},"packet_summary":{"streams":[]},"pcm_summary":{"streams":[]},
        "display_call":{"id":"CALL-001","started_at":100.0,"status":"TERMINATED"},"analysis_context":{"analysis_mode":"OFFLINE_IMPORTED","reconstructed_call_count":1,"call_scope":"BOUND","call_origin":"RECONSTRUCTED_FROM_PCAP"},
        "multi_call_summary":{"call_count":0,"finding_groups":[]},"ab_comparison":[],"normal_and_exclusion_evidence":[],"artifacts":[],
    }

    rendered=render_report_html(payload)

    assert "T+10.146s" in rendered
    assert "/api/v1/artifacts/img-1/content" in rendered
    assert "<audio controls" in rendered
    assert "/api/v1/artifacts/audio-1/content" in rendered
    assert "Frame" in rendered and "20272" in rendered and "46511" in rendered
    assert "当前不能确认什么" in rendered
    assert payload["findings"][0]["evidence_card"]["audio_evidence"]["status"]=="AVAILABLE"
    assert payload["evidence_card_summary"]["audio_available_count"]==1


def test_attach_evidence_cards_keeps_one_card_per_finding_and_reports_missing_audio_count():
    payload={"display_call":{"started_at":100.0},"findings":[_high_delta_finding(with_audio=False)]}
    attach_evidence_cards(payload)
    assert len(payload["evidence_cards"])==1
    assert payload["evidence_card_summary"]["finding_count"]==1
    assert payload["evidence_card_summary"]["audio_expected_count"]==1
    assert payload["evidence_card_summary"]["audio_unavailable_count"]==1

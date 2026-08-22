from __future__ import annotations

import copy

from app.reports.report_grounding import FAIL, PASS, PASS_WITH_WARNINGS, apply_report_grounding, validate_report_grounding


def _valid_high_delta() -> dict:
    visual={
        "artifact_id":"img-1","type":"RTP_TIMELINE_PNG","filename":"high-delta.png","content_type":"image/png","role":"FINDING",
        "metadata":{"annotation_complete":True,"annotation_contract":{"title":"HIGH_DELTA","caption":"focused timeline"}},
    }
    audio={
        "artifact_id":"audio-1","type":"AUDIO_CLIP","filename":"high-delta.wav","content_type":"audio/wav","role":"FINDING",
        "metadata":{"event_type":"HIGH_DELTA","event_time":110.146,"stream_id":"rtp-up"},
    }
    return {
        "finding_id":"finding-1","stable_key":"stable-1","finding_signature":"high_delta|rtp|up","type":"HIGH_DELTA","severity":"MEDIUM","evidence_level":"L2",
        "title":"RTP 包间隔异常增大（HIGH_DELTA）","observation":"DUT 上行 RTP 共观测到 2 次包间隔异常。",
        "interpretation":"Sequence 连续，因此属于 Delay/Stall，不应写成 Packet Loss。",
        "root_cause_boundary":"不能单凭 HIGH_DELTA 区分 DUT 调度、网络排队或抓包观察点。",
        "time_range":{"start":110.146,"end":110.175,"representative":110.146},
        "scope":{"layer":"RTP","call_id":"CALL-001","rtp_stream_id":"rtp-up","direction":"DUT_TO_PBX","ssrc":77},
        "metrics":{
            "event_count":2,"max_delta_ms":175.043,"ptime_ms":20.0,"max_excess_delay_ms":155.043,
            "stream_lost_packets":0,"all_sequence_continuous":True,
            "events":[
                {"time":110.146,"previous_frame_number":20272,"current_frame_number":20285,"previous_sequence":46511,"current_sequence":46512,"delta_ms":146.083,"sequence_continuous":True,"classification":"INTERARRIVAL_STALL_WITHOUT_RTP_GAP"},
                {"time":110.175,"previous_frame_number":20329,"current_frame_number":20344,"previous_sequence":46519,"current_sequence":46520,"delta_ms":175.043,"sequence_continuous":True,"classification":"INTERARRIVAL_STALL_WITHOUT_RTP_GAP"},
            ],
        },
        "semantic_summary":{"loss_interpretation":"DELAY_NOT_PACKET_LOSS","all_sequence_continuous":True},
        "artifact_refs":[visual,audio],"evidence_refs":[{"type":"PCAP","id":"pcap-1"}],"event_refs":[{"source":"packet.anomalies","index":1}],"source_analyzer_run_ids":["run-packet"],
        "evidence_card":{
            "finding_id":"finding-1","finding_type":"HIGH_DELTA","what_happened":"DUT 上行 RTP 共观测到 2 次包间隔异常。",
            "root_cause_boundary":"不能单凭 HIGH_DELTA 区分 DUT 调度、网络排队或抓包观察点。","next_action":"对齐 PCM/反向 RTP。",
            "time":{"representative_utc":"1970-01-01T00:01:50.146Z"},
            "packet_refs":[{"event":1,"previous_frame":20272,"current_frame":20285,"previous_seq":46511,"current_seq":46512}],
            "visual_evidence":[{"artifact_id":"img-1","type":"RTP_TIMELINE_PNG","annotation_complete":True}],
            "audio_evidence":{"status":"AVAILABLE","reason":None,"clips":[{"artifact_id":"audio-1","type":"AUDIO_CLIP"}]},
        },
    }


def _payload() -> dict:
    finding=_valid_high_delta()
    return {
        "schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v3",
        "analysis_context":{"analysis_mode":"OFFLINE_IMPORTED","reconstructed_call_count":1,"call_scope":"BOUND","call_selection_status":"SELECTED"},
        "session":None,"display_call":{"id":"CALL-001","dialed_number":"601"},
        "packet_summary":{"call_count":2,"streams":[{"stream_id":"rtp-up","lost_packets":0}]},
        "completeness":{"state":"COMPLETE","reviewability":"FULLY_REVIEWABLE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True}},
        "findings":[finding],
        "artifacts":[
            {"artifact_id":"img-1","type":"RTP_TIMELINE_PNG","filename":"high-delta.png"},
            {"artifact_id":"audio-1","type":"AUDIO_CLIP","filename":"high-delta.wav"},
        ],
        "ab_comparison":[],
    }


def _codes(validation:dict)->set[str]:
    return {x["code"] for x in validation["issues"]}


def test_valid_grounded_report_passes_and_claim_manifest_is_traceable():
    payload=_payload()
    validation=apply_report_grounding(payload,raise_on_blocker=False)

    assert validation["status"]==PASS
    assert validation["reviewability_status"]=="FULLY_REVIEWABLE"
    assert payload["claim_manifest"]["claim_count"]==1
    claim=payload["claim_manifest"]["claims"][0]
    assert claim["finding_ref"]=="finding-1"
    assert claim["artifact_refs"]==["img-1","audio-1"]
    assert claim["metrics"]["max_delta_ms"]==175.043
    assert payload["completeness"]["grounding_status"]==PASS


def test_bound_reconstructed_call_cannot_disappear_from_report():
    payload=_payload();payload["display_call"]=None
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "CALL_BINDING_CONTRADICTION" in _codes(validation)


def test_offline_import_cannot_bind_reproduction_session():
    payload=_payload();payload["session"]={"id":"runtime-session"}
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "OFFLINE_SESSION_CONTRADICTION" in _codes(validation)


def test_high_delta_with_continuous_sequence_must_not_be_semantically_labeled_as_loss():
    payload=_payload();payload["findings"][0]["semantic_summary"]["loss_interpretation"]="PACKET_LOSS"
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "HIGH_DELTA_LOSS_SEMANTIC_CONTRADICTION" in _codes(validation)


def test_packet_loss_finding_requires_positive_loss_or_sequence_gap_evidence():
    payload=_payload();finding=payload["findings"][0]
    finding["type"]="PACKET_LOSS";finding["metrics"]={"lost_packets":0,"stream_lost_packets":0};finding["semantic_summary"]={}
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "PACKET_LOSS_WITHOUT_LOSS_EVIDENCE" in _codes(validation)


def test_periodic_signal_cannot_confirm_physical_root_cause():
    payload=_payload();finding=payload["findings"][0]
    finding["type"]="LOCAL_CAPTURE_PERIODIC_INTERFERENCE";finding["observation"]="已确认电源根因已确认。";finding["root_cause_boundary"]="已定位完成"
    finding["evidence_card"]["root_cause_boundary"]="已定位完成"
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "PERIODIC_ROOT_CAUSE_BOUNDARY_MISSING" in _codes(validation)
    assert "PERIODIC_PHYSICAL_ROOT_CAUSE_OVERCLAIM" in _codes(validation)


def test_finding_artifact_ref_must_exist_in_canonical_inventory():
    payload=_payload();payload["artifacts"]=[payload["artifacts"][0]]
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "ARTIFACT_REF_NOT_FOUND" in _codes(validation)


def test_high_delta_requires_visual_and_frame_seq_drilldown():
    payload=_payload();card=payload["findings"][0]["evidence_card"]
    card["visual_evidence"]=[];card["packet_refs"]=[]
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert {"PRIMARY_VISUAL_MISSING","PACKET_FRAME_TRACE_MISSING"}.issubset(_codes(validation))


def test_audio_unavailable_with_explicit_reason_is_warning_and_downgrades_reviewability():
    payload=_payload();card=payload["findings"][0]["evidence_card"]
    card["audio_evidence"]={"status":"UNAVAILABLE","reason":"NO_MATCHING_ANOMALY_AUDIO_CLIP","clips":[]}
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==PASS_WITH_WARNINGS
    assert validation["reviewability_status"]=="PARTIALLY_REVIEWABLE"
    assert "AUDIO_EVIDENCE_UNAVAILABLE" in _codes(validation)


def test_audio_available_cannot_point_to_full_raw_wav():
    payload=_payload();card=payload["findings"][0]["evidence_card"]
    card["audio_evidence"]={"status":"AVAILABLE","reason":None,"clips":[{"artifact_id":"raw","type":"AUDIO_WAV"}]}
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert "AUDIO_STATUS_WITHOUT_SAFE_CLIP" in _codes(validation)


def test_validator_does_not_mutate_diagnostic_truth_when_called_directly():
    payload=_payload();before=copy.deepcopy(payload["findings"])
    validation=validate_report_grounding(payload)
    assert validation["status"]==PASS
    assert payload["findings"]==before

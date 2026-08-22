from __future__ import annotations

from app.reports.report_grounding import FAIL, PASS, apply_report_grounding


def _base_finding() -> dict:
    return {
        "stable_key":"stable-replay","finding_signature":"high_delta|rtp|up","type":"HIGH_DELTA","severity":"MEDIUM","evidence_level":"L2",
        "title":"RTP 包间隔异常增大（HIGH_DELTA）","observation":"上行 RTP 出现短时节奏停顿。",
        "interpretation":"Sequence 连续，因此属于 Delay/Stall，不是 Packet Loss。",
        "root_cause_boundary":"不能单凭 HIGH_DELTA 确认 DUT 调度、网络或抓包观察点根因。",
        "time_range":{"start":10.1,"end":10.2,"representative":10.1},
        "scope":{"layer":"RTP","rtp_stream_id":"up"},
        "metrics":{"stream_lost_packets":0,"all_sequence_continuous":True,"events":[{"sequence_continuous":True}]},
        "semantic_summary":{"loss_interpretation":"DELAY_NOT_PACKET_LOSS"},
        "artifact_refs":[],
    }


def _payload(finding:dict)->dict:
    return {
        "analysis_context":{"analysis_mode":"OFFLINE_IMPORTED","reconstructed_call_count":1,"call_scope":"BOUND","call_selection_status":"SELECTED"},
        "session":None,"display_call":{"id":"CALL-001"},"packet_summary":{"call_count":2},
        "completeness":{"state":"COMPLETE","reviewability":"FULLY_REVIEWABLE"},
        "findings":[finding],"artifacts":[],"ab_comparison":[],
    }


def test_in_memory_replay_keeps_semantic_gate_but_skips_report_artifact_publication_gate():
    finding=_base_finding()  # no persisted finding_id
    payload=_payload(finding)
    validation=apply_report_grounding(payload,raise_on_blocker=False)

    assert validation["status"]==PASS
    assert validation["validation_scope"]=="IN_MEMORY_REPLAY"
    assert validation["publication_finding_count"]==0

    finding["semantic_summary"]["loss_interpretation"]="PACKET_LOSS"
    validation=apply_report_grounding(payload,raise_on_blocker=False)
    assert validation["status"]==FAIL
    assert any(x["code"]=="HIGH_DELTA_LOSS_SEMANTIC_CONTRADICTION" for x in validation["issues"])


def test_safe_periodic_unknown_boundary_does_not_trigger_physical_root_cause_overclaim():
    finding=_base_finding()
    finding.update({
        "type":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
        "title":"本地采集链路周期性干扰",
        "observation":"PCM_RX 与上行 RTP 均存在周期结构。",
        "interpretation":"当前只能确认本地上行采集路径已经可观察到异常，不能确认电源、接地或 SLIC 根因。",
        "root_cause_boundary":"不能单独确认电源、接地、话柄、线路、FXS/SLIC 等物理根因，需 A/B 验证。",
        "metrics":{"pcm_rx":{"level":"HIGH"}},
        "semantic_summary":{},
    })
    validation=apply_report_grounding(_payload(finding),raise_on_blocker=False)

    assert validation["status"]==PASS
    assert not any(x["code"]=="PERIODIC_PHYSICAL_ROOT_CAUSE_OVERCLAIM" for x in validation["issues"])

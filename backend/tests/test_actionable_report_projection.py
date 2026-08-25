from __future__ import annotations

import json
from types import SimpleNamespace

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.reports.actionable_summary import attach_actionable_summary


def _payload() -> dict:
    return {
        "headline": "本地音频采集链路存在稳定周期性干扰并进入上行RTP",
        "case": {"case_no": "OFFLINE-GOLDEN-001"},
        "analysis_context": {
            "analysis_mode": "OFFLINE_IMPORTED",
            "reconstructed_call_count": 2,
        },
        "display_call": {
            "id": "CALL-001",
            "sip_call_id": "00ad1c804c33b255@192.168.3.200",
            "status": "TERMINATED",
            "caller": "8000",
            "dialed_number": "601",
            "started_at": "2026-08-14T07:02:49.100710+00:00",
            "ended_at": "2026-08-14T07:03:40.556065+00:00",
            "media_started_at": "2026-08-14T07:02:52.055640+00:00",
            "media_ended_at": "2026-08-14T07:03:40.535864+00:00",
        },
        "diagnosis": {
            "state": "DIAGNOSED",
            "summary": {
                "headline": "本地音频采集链路存在稳定周期性干扰并进入上行RTP"
            },
            "hypotheses": [
                {
                    "code": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
                    "title": "本地音频采集链路存在稳定周期性干扰并进入上行RTP",
                    "status": "SUPPORTED",
                    "confidence": 0.96,
                    "fault_domain": "Audio/Analog",
                }
            ],
            "known": ["周期干扰已进入上行RTP"],
            "unknown": ["具体硬件节点尚未闭环"],
            "excluded": ["PBX/下行网络不是当前持续周期底噪的主要引入点"],
            "plan": [
                {
                    "action_type": "REQUEST_USER_EVIDENCE",
                    "reason": "A/B区分具体硬件节点",
                    "risk_level": "USER",
                    "auto_execute": False,
                    "params": {"purpose": "close_specific_hardware_root_cause"},
                    "priority": 40,
                },
                {
                    "action_type": "REQUEST_MULTI_POINT_PCAP",
                    "reason": "定位RTP抖动产生区间",
                    "risk_level": "USER",
                    "auto_execute": False,
                    "params": {"purpose": "locate_jitter_segment"},
                    "priority": 65,
                },
            ],
        },
        "preliminary_assessment": {},
        "evidence_boundary": {"statement": "初步证据，不确认最终硬件根因。"},
        "completeness": {
            "state": "COMPLETE",
            "reviewability": "FULLY_REVIEWABLE",
            "capture": {"pcap": True, "pcm_rx": True, "pcm_tx": True},
        },
        "findings": [],
        "highest_severity": "HIGH",
        "normal_and_exclusion_evidence": [],
    }


def test_actionable_summary_exposes_scope_absolute_time_and_plan() -> None:
    payload = _payload()
    attach_actionable_summary(payload, payload["diagnosis"])

    assert payload["problem_scope"]["affected_path"] == "被测设备本地音频采集链路（PCM RX → 上行 RTP）"
    assert "PBX/下行网络" in payload["problem_scope"]["excluded_or_weakened"][0]

    window = payload["observation_window"]
    assert window["scope"] == "ACTIVE_MEDIA_WINDOW"
    assert window["absolute_start_utc"] == "2026-08-14T07:02:52.055640+00:00"
    assert window["absolute_end_utc"] == "2026-08-14T07:03:40.535864+00:00"
    assert window["absolute_start_local"] == "2026-08-14T15:02:52.055640+08:00"
    assert window["absolute_end_local"] == "2026-08-14T15:03:40.535864+08:00"
    assert window["exact_event_window_known"] is False

    assert len(payload["next_actions"]) == 2
    p0 = payload["next_actions"][0]
    assert p0["priority"] == 40
    assert "恢复基线A" in " ".join(p0["execution_steps"])
    assert "B→A" in p0["acceptance_criteria"]
    assert payload["preliminary_assessment"]["recommended_next_action"] == "A/B区分具体硬件节点"


def test_feishu_projection_renders_actionable_sections() -> None:
    payload = _payload()
    report = SimpleNamespace(
        version=1,
        status="COMPLETE",
        scope_type="CASE",
        id="report-1",
    )
    service = FeishuEvidenceDocumentService(transport=object(), storage=object())

    blocks, _, _ = service._core_blocks(report, payload)
    raw = json.dumps(blocks, ensure_ascii=False)

    assert "1.1 问题范围与绝对时间" in raw
    assert "被测设备本地音频采集链路（PCM RX → 上行 RTP）" in raw
    assert "本地绝对时间（UTC+8）" in raw
    assert "2026-08-14T15:02:52.055640+08:00" in raw
    assert "2026-08-14T15:03:40.535864+08:00" in raw
    assert "精确异常首末时刻已知：否" in raw

    assert "1.2 下一步建议 / 验证顺序 / 通过标准" in raw
    assert "A/B区分具体硬件节点" in raw
    assert "定位RTP抖动产生区间" in raw
    assert "通过标准" in raw
    assert "B→A" in raw

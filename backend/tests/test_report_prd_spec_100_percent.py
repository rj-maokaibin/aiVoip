from __future__ import annotations

import json
from types import SimpleNamespace

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.reports.evidence_brief import render_report_html
from app.reports.prd_spec_v1_alignment import D112_ORDER, finalize_report_contract


FORBIDDEN = (
    "范围：未绑定",
    "None ～ None",
    "下一步建议：None",
    "Evidence Card: None",
    "【修订版｜问题范围/绝对时间/下一步｜master固定版】",
    "下方旧版内容保留作为历史记录，若有冲突以本修订版为准",
)


def _artifact(artifact_id: str, artifact_type: str, *, audio: bool = False) -> dict:
    return {
        "artifact_id": artifact_id,
        "type": artifact_type,
        "filename": f"{artifact_id}.{'wav' if audio else 'png'}",
        "content_type": "audio/wav" if audio else "image/png",
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "local_path": f"/tmp/{artifact_id}.{'wav' if audio else 'png'}",
        "created_at": "2026-08-23T00:00:00+00:00",
        "metadata": {
            "source_artifact_ids": [],
            "analyzer_name": "media_intelligence",
            "analyzer_version": "0.5.0",
            "profile_version": "test-v1",
            "annotation_complete": not audio,
            "annotation_contract": {
                "caption": f"{artifact_type} exact Finding evidence",
                "title": artifact_type,
            } if not audio else {},
            "created_at": "2026-08-23T00:00:00+00:00",
        },
    }


def _payload() -> dict:
    spectrum = _artifact("spectrum-1", "SPECTRUM_PNG")
    waveform = _artifact("waveform-1", "WAVEFORM_PNG")
    audio = _artifact("periodic-audio-1", "PERIODIC_AUDIO_CLIP", audio=True)
    bundle = {
        "artifact_id": "bundle-1",
        "type": "EVIDENCE_BUNDLE",
        "filename": "evidence-bundle-internal-full-v2.zip",
        "content_type": "application/zip",
        "sha256": "b" * 64,
        "size_bytes": 4567,
        "local_path": "/tmp/evidence-bundle-internal-full-v2.zip",
        "created_at": "2026-08-23T00:00:00+00:00",
        "metadata": {
            "source_artifact_ids": [],
            "analyzer_name": "evidence_bundle_builder",
            "analyzer_version": "v1",
            "profile_version": "INTERNAL_FULL",
            "created_at": "2026-08-23T00:00:00+00:00",
        },
    }
    manifest = {
        **bundle,
        "artifact_id": "manifest-1",
        "type": "MANIFEST_JSON",
        "filename": "manifest.json",
        "content_type": "application/json",
        "sha256": "c" * 64,
        "size_bytes": 999,
        "local_path": "/tmp/manifest.json",
    }
    refs = [
        {**spectrum, "role": "FINDING"},
        {**waveform, "role": "FINDING"},
        {**audio, "role": "FINDING"},
    ]
    return {
        "schema_version": "preliminary-evidence-report-v1",
        "composer_version": "test",
        "report_version": 1,
        "generated_at": "2026-08-23T00:00:00+00:00",
        "scope": {"type": "CASE", "id": "case-1"},
        "case": {"id": "case-1", "case_no": "CASE-001", "summary": "持续周期底噪"},
        "analysis_context": {
            "analysis_mode": "OFFLINE_IMPORTED",
            "call_scope": "BOUND",
            "call_selection_status": "SELECTED",
            "reconstructed_call_count": 1,
            "diagnostic_call_count": 1,
        },
        "display_call": {
            "id": "CALL-001",
            "status": "TERMINATED",
            "caller": "8000",
            "dialed_number": "601",
            "started_at": "2026-08-14T07:02:49.100710+00:00",
            "ended_at": "2026-08-14T07:03:40.556065+00:00",
            "media_started_at": "2026-08-14T07:02:52.055640+00:00",
            "media_ended_at": "2026-08-14T07:03:40.535864+00:00",
        },
        "packet_summary": {
            "available": True,
            "packet_count": 1000,
            "sip_message_count": 10,
            "call_count": 1,
            "rtp_stream_count": 2,
            "calls": [{"call_id": "sip-1"}],
            "streams": [
                {"stream_id": "rtp-up", "source": "dut", "destination": "gw", "loss_rate": 0.0},
                {"stream_id": "rtp-down", "source": "gw", "destination": "dut", "loss_rate": 0.0},
            ],
        },
        "pcm_summary": {
            "available": True,
            "streams": [
                {"tap": {"name": "pcm_rx", "direction": "RX"}, "sessions": [{"session_index": 0}]},
                {"tap": {"name": "pcm_tx", "direction": "TX"}, "sessions": [{"session_index": 0}]},
            ],
        },
        "completeness": {
            "state": "COMPLETE",
            "capture": {"pcap": True, "pcm_rx": True, "pcm_tx": True, "debug": False},
            "analyzers": {
                "packet": {"available": True},
                "pcm": {"available": True},
                "media": {"available": True},
            },
        },
        "findings": [
            {
                "stable_key": "local-periodic-1",
                "finding_signature": "local_capture_periodic_interference|pcm_rx|periodic|local_capture_path|sig-v1",
                "type": "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
                "status": "OBSERVED",
                "severity": "HIGH",
                "evidence_level": "L2",
                "title": "本地采集链路周期性干扰",
                "observation": "PCM RX 已出现约 20ms 周期特征，并传播进入上行 RTP。",
                "interpretation": "当前证据支持被测设备本地采集链路证据边界。",
                "root_cause_boundary": "当前不能确认电源、接地、话柄、线路或 SLIC 中的具体物理根因，需 A/B-A 验证。",
                "time_range": {"start": None, "end": None, "representative": None},
                "scope": {},
                "metrics": {
                    "pcm_rx": {"level": "HIGH", "representative": {"autocorrelation": {"20ms": 0.92}}, "comb": {"hit_count": 6}},
                    "upstream_rtp": {"representative": {"autocorrelation": {"20ms": 0.85}}},
                    "downstream_rtp": {"representative": {"autocorrelation": {"20ms": 0.10}}},
                    "strength": {"pcm_rx": 0.95, "upstream_rtp": 0.88, "downstream_rtp": 0.12},
                },
                "evidence_refs": [{"type": "ANALYZER_RUN", "id": "media-run-1"}],
                "event_refs": [{"source": "media.periodic", "index": 0}],
                "artifact_refs": refs,
                "correlation": {},
            }
        ],
        "finding_count": 1,
        "highest_severity": "HIGH",
        "normal_and_exclusion_evidence": [{"type": "RTP_NO_LOSS", "text": "RTP 未检测到丢包。"}],
        "evidence_boundary": {"statement": "初步证据，不确认最终物理根因。"},
        "preliminary_assessment": {},
        "artifacts": [spectrum, waveform, audio, bundle, manifest],
        "diagnosis": {
            "state": "DIAGNOSED",
            "summary": {"headline": "本地音频采集链路存在稳定周期性干扰并进入上行RTP"},
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
            "excluded": ["PBX/下行网络不是持续周期底噪的主要引入点"],
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
        "evidence_bundle_summary": {
            "status": "AVAILABLE",
            "profile": "INTERNAL_FULL",
            "manifest_schema": "evidence-bundle-v1",
        },
    }


def _report() -> SimpleNamespace:
    return SimpleNamespace(
        id="report-2",
        case_id="case-1",
        session_id=None,
        call_id="CALL-001",
        scope_type="CASE",
        scope_id="case-1",
        version=2,
        status="COMPOSING",
    )


def _block_text(block: dict) -> str:
    for key in ("text", "heading1", "heading2", "heading3", "bullet"):
        body = block.get(key)
        if body:
            return "".join(str((x.get("text_run") or {}).get("content") or "") for x in body.get("elements") or [])
    return ""


def test_canonical_v2_finding_contract_is_self_contained() -> None:
    payload = _payload()
    report = _report()
    finalize_report_contract(report, payload)

    assert payload["version"] == 2
    assert payload["status"] == "COMPLETE"
    assert payload["projection_contract"]["single_canonical_fact_layer"] is True
    assert payload["projection_contract"]["legacy_prepend_revision_allowed"] is False

    dimensions = payload["capture_quality"]["dimensions"]
    assert list(dimensions) == ["PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION"]
    assert dimensions["DEBUG"]["requirement"] == "OPTIONAL"
    assert dimensions["DEBUG"]["available"] is False
    assert payload["capture_quality"]["state"] == "COMPLETE"

    finding = payload["findings"][0]
    assert finding["scope_binding_status"] == "BOUND"
    assert finding["scope"]["call_id"] == "CALL-001"
    assert finding["scope"]["pcm_tap"] == "pcm_rx"
    assert finding["scope"]["layer"] == "PCM_RX_TO_RTP_UPSTREAM"
    assert finding["time_range"]["semantics"] == "OBSERVATION_BOUNDARY"
    assert finding["time_range"]["exact_event_window_known"] is False
    assert finding["time_range"]["start"] == "2026-08-14T07:02:52.055640+00:00"
    assert finding["next_action"] == "A/B区分具体硬件节点"
    assert "恢复基线A" in " ".join(finding["action_contract"]["execution_steps"])
    assert "B→A" in finding["verification_acceptance"]

    card = finding["evidence_card"]
    assert card["scope"]["binding_status"] == "BOUND"
    assert card["time"]["exact_event_window_known"] is False
    assert card["verification_acceptance"] == finding["verification_acceptance"]
    assert {x["type"] for x in card["visual_evidence"]} >= {"SPECTRUM_PNG", "WAVEFORM_PNG"}
    assert card["audio_evidence"]["status"] == "AVAILABLE"
    assert payload["evidence_card_summary"]["cards_with_acceptance"] == 1
    assert payload["artifact_provenance_status"]["complete"] is True
    assert payload["completeness"]["reviewability"] == "FULLY_REVIEWABLE"


def test_feishu_and_html_share_d112_and_no_legacy_truth_layer() -> None:
    payload = _payload()
    report = _report()
    finalize_report_contract(report, payload)

    service = FeishuEvidenceDocumentService(transport=object(), storage=object())
    history = [service._text("历史 V1 已归档为审计快照；V2 正文不混排旧结论。", 12)]
    blocks, _, _ = service._core_blocks(report, payload, history_blocks=history)
    text = "\n".join(_block_text(block) for block in blocks if _block_text(block))
    offsets = [text.index(title) for title in D112_ORDER]
    assert offsets == sorted(offsets)
    for forbidden in FORBIDDEN:
        assert forbidden not in text
    assert "A/B 硬件变量验证" in text
    assert "多点 PCAP 链路定位" in text
    assert "REQUEST_USER_EVIDENCE" not in text
    assert "risk=USER" not in text
    assert "验证步骤：基线A" in text
    assert "通过标准" in text
    assert "B→A" in text
    assert "PCAP：可用｜必需" in text
    assert "DEBUG：缺失/不可用｜可选" in text
    assert "Evidence Bundle：已生成" in text
    assert "Manifest：已生成" in text

    rendered = render_report_html(payload)
    for forbidden in FORBIDDEN:
        assert forbidden not in rendered
    offsets = [rendered.index(f">{title}<") for title in D112_ORDER]
    assert offsets == sorted(offsets)
    assert "观察边界（非精确异常区间）" in rendered
    assert "验证通过标准" in rendered
    assert "B→A" in rendered
    assert "PCM_RX" in rendered and "CORRELATION" in rendered
    assert "Evidence Bundle：已生成" in rendered


def test_root_cause_authority_is_not_upgraded_by_v2_projection() -> None:
    payload = _payload()
    report = _report()
    finalize_report_contract(report, payload)
    finding = payload["findings"][0]
    text = " ".join([
        str(finding.get("observation") or ""),
        str(finding.get("interpretation") or ""),
        str(finding.get("root_cause_boundary") or ""),
    ])
    assert "当前不能确认" in text
    assert "电源根因已确认" not in text
    assert "接地根因已确认" not in text
    assert "SLIC根因已确认" not in text

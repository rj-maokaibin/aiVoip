from types import SimpleNamespace

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService


class _Fake:
    pass


def _block_text(block:dict)->str:
    for key in ("text","heading1","heading2","heading3","bullet"):
        node=block.get(key)
        if node:
            return "".join((x.get("text_run") or {}).get("content","") for x in node.get("elements",[]))
    return ""


def _finding_with_card()->dict:
    return {
        "finding_id":"finding-1","severity":"HIGH","title":"周期性干扰","evidence_level":"L2","observation":"PCM RX异常","interpretation":"异常首次可观测于PCM_RX。",
        "root_cause_boundary":"不能直接确认SLIC。","time_range":{"start":1,"end":2},"correlation":{"first_observable_boundary":{"status":"OBSERVED_BOUNDARY","statement":"异常首次可观测于 PCM_RX；这是证据边界。"}},
        "evidence_card":{
            "version":"evidence-card-v1","finding_id":"finding-1","what_happened":"PCM RX低能量窗口存在稳定周期性干扰。","initial_interpretation":"异常可观察于本地上行采集路径。",
            "scope":{"layer":"PCM_RX","pcm_tap":"pcm_rx","direction":"RX","call_id":"CALL-001"},
            "time":{"absolute_start_utc":"2026-08-14T07:02:44.323Z","absolute_end_utc":"2026-08-14T07:02:46.323Z","call_relative_representative":"T+1.200s"},
            "measurements":[{"label":"20ms 自相关","value":0.91},{"label":"频梳命中","value":7,"unit":"peaks"}],
            "packet_refs":[],
            "visual_evidence":[{"artifact_id":"img-1","type":"SPECTRUM_PNG","caption":"pcm_rx 周期干扰频谱"}],
            "audio_evidence":{"status":"AVAILABLE","reason":None,"clips":[{"artifact_id":"aud-1","type":"PERIODIC_AUDIO_CLIP","caption":"pcm_rx representative clip"}]},
            "root_cause_boundary":"不能直接确认SLIC。","next_action":"执行话柄/线路/FXS-SLIC/供电接地 A/B。",
        },
    }


def test_feishu_document_uses_d112_order_and_places_key_media_under_finding_before_attachment_section():
    service=FeishuEvidenceDocumentService(transport=_Fake(),storage=_Fake())
    report=SimpleNamespace(id="report-1",version=3,status="COMPLETE",scope_type="CASE")
    payload={
        "generated_at":"2026-08-18T01:00:00+00:00","case":{"case_no":"CASE-1"},"completeness":{"state":"COMPLETE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True}},
        "highest_severity":"HIGH","headline":"发现 1 个问题","evidence_boundary":{"statement":"不确认最终根因。"},"findings":[_finding_with_card()],
        "evidence_card_summary":{"version":"evidence-card-v1","finding_count":1,"audio_expected_count":1,"audio_available_count":1,"audio_unavailable_count":0},
        "call":{"call_no":2,"status":"ENDED","started_at":"a","ended_at":"b"},
        "multi_call_summary":{"call_count":5,"finding_groups":[{"severity":"HIGH","title":"周期性干扰","occurrence_calls":5,"total_calls":5,"reproduction_rate":1.0,"stability":"STABLE"}]},
        "ab_comparison":[],"normal_and_exclusion_evidence":[{"text":"RTP双向存在"}],"schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v2",
    }
    blocks,attachment_index,plan=service._core_blocks(report,payload)
    text=[_block_text(x) for x in blocks]
    expected=["0. 当前状态 / 快速导航","1. 当前初步结论","2. 当前重点问题","3. 证据完整度","4. 最新一次复现结果","5. 多次复现汇总","6. A/B 对比","7. 历次 Reproduction Session（复现会话）","8. 正常项 / 排除性证据","9. 完整技术证据","10. Evidence Bundle / 附件","11. 报告版本与审计记录"]
    positions=[text.index(x) for x in expected]

    assert positions==sorted(positions)
    assert positions[-2] < attachment_index == positions[-1]
    assert any("首次可观测于 PCM_RX" in x for x in text)
    assert any("20ms 自相关：0.91" in x for x in text)
    assert any("T+1.200s" in x for x in text)
    assert any("下一步建议" in x and "A/B" in x for x in text)
    assert any("5/5" in x and "100.0%" in x for x in text)
    assert {x["artifact_id"] for x in plan}=={"img-1","aud-1"}
    assert all(x["block_index"] < positions[3] for x in plan)
    assert next(x for x in plan if x["artifact_id"]=="img-1")["is_image"] is True
    assert next(x for x in plan if x["artifact_id"]=="aud-1")["is_image"] is False


def test_feishu_document_projects_offline_call_as_reconstructed_not_reproduction():
    service=FeishuEvidenceDocumentService(transport=_Fake(),storage=_Fake())
    report=SimpleNamespace(id="report-offline",version=1,status="COMPLETE",scope_type="CASE")
    payload={
        "generated_at":"2026-08-20T00:00:00+00:00","case":{"case_no":"CASE-OFFLINE"},
        "completeness":{"state":"COMPLETE","reviewability":"FULLY_REVIEWABLE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True}},
        "highest_severity":"HIGH","headline":"发现周期性干扰","evidence_boundary":{"statement":"不确认最终根因。"},"findings":[],
        "analysis_context":{"analysis_mode":"OFFLINE_IMPORTED","call_origin":"RECONSTRUCTED_FROM_PCAP","call_scope":"BOUND","reconstructed_call_count":1},
        "display_call":{"id":"CALL-001","sip_call_id":"00ad1c804c33b255@192.168.3.200","status":"TERMINATED","started_at":"2026-08-14T07:02:49+00:00","ended_at":"2026-08-14T07:03:40+00:00","caller":"8000","dialed_number":"601"},
        "call":{"id":"CALL-001","sip_call_id":"00ad1c804c33b255@192.168.3.200","status":"TERMINATED","caller":"8000","dialed_number":"601"},
        "multi_call_summary":{"call_count":0,"finding_groups":[]},"ab_comparison":[],"normal_and_exclusion_evidence":[],
        "schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v2","evidence_card_summary":{"version":"evidence-card-v1","finding_count":0,"audio_expected_count":0,"audio_available_count":0,"audio_unavailable_count":0},
    }

    blocks,_,plan=service._core_blocks(report,payload);text=[_block_text(x) for x in blocks]

    # D112 is the current PRD/SPEC authority for the living V2 projection. The
    # offline-specific reconstructed-Call heading is additive inside section 4;
    # it must not replace the frozen D112 section title.
    assert "4. 最新一次复现结果" in text
    assert "4. 当前离线 Call 重建结果" in text
    assert any("分析方式：离线证据导入" in x and "复现 Session：不适用" in x for x in text)
    assert any("Call：CALL-001" in x and "SIP Call-ID：00ad1c804c33b255@192.168.3.200" in x for x in text)
    assert any("号码：8000 → 601" in x for x in text)
    assert any("当前离线重建 Call 仅用于证据展示，不计入 Reproduction Call 复现次数" in x for x in text)
    assert any("不会为了填充报告创建 ReproductionSession/ReproductionCall" in x for x in text)
    assert not any("Call：None" in x for x in text)
    assert plan==[]


def test_feishu_card_surfaces_explicit_audio_unavailable_reason_instead_of_omitting_it():
    service=FeishuEvidenceDocumentService(transport=_Fake(),storage=_Fake())
    report=SimpleNamespace(id="report-2",version=1,status="PARTIAL_COMPLETE",scope_type="CASE")
    finding=_finding_with_card();finding["evidence_card"]["audio_evidence"]={"status":"UNAVAILABLE","reason":"NO_MATCHING_ANOMALY_AUDIO_CLIP"}
    payload={"generated_at":"x","case":{"case_no":"CASE-2"},"completeness":{"state":"PARTIAL","capture":{}},"highest_severity":"HIGH","headline":"x","evidence_boundary":{"statement":"x"},
             "findings":[finding],"call":{},"multi_call_summary":{},"ab_comparison":[],"normal_and_exclusion_evidence":[],"schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v2"}

    blocks,_,plan=service._core_blocks(report,payload);text=[_block_text(x) for x in blocks]
    assert any("异常音频：暂不可用" in x and "NO_MATCHING_ANOMALY_AUDIO_CLIP" in x for x in text)
    assert {x["artifact_id"] for x in plan}=={"img-1"}

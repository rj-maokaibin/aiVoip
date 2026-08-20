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


def test_feishu_document_uses_d112_order_and_attachment_slot_before_audit():
    service=FeishuEvidenceDocumentService(transport=_Fake(),storage=_Fake())
    report=SimpleNamespace(id="report-1",version=3,status="COMPLETE",scope_type="CASE")
    payload={
        "generated_at":"2026-08-18T01:00:00+00:00","case":{"case_no":"CASE-1"},"completeness":{"state":"COMPLETE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True}},
        "highest_severity":"HIGH","headline":"发现 1 个问题","evidence_boundary":{"statement":"不确认最终根因。"},
        "findings":[{"severity":"HIGH","title":"周期性干扰","evidence_level":"L2","observation":"PCM RX异常","interpretation":"异常首次可观测于PCM_RX。",
                     "root_cause_boundary":"不能直接确认SLIC。","time_range":{"start":1,"end":2},"correlation":{"first_observable_boundary":{"status":"OBSERVED_BOUNDARY","statement":"异常首次可观测于 PCM_RX；这是证据边界。"}}}],
        "call":{"call_no":2,"status":"ENDED","started_at":"a","ended_at":"b"},
        "multi_call_summary":{"call_count":5,"finding_groups":[{"severity":"HIGH","title":"周期性干扰","occurrence_calls":5,"total_calls":5,"reproduction_rate":1.0,"stability":"STABLE"}]},
        "ab_comparison":[],"normal_and_exclusion_evidence":[{"text":"RTP双向存在"}],"schema_version":"preliminary-evidence-report-v1","composer_version":"evidence-brief-composer-v1",
    }
    blocks,attachment_index=service._core_blocks(report,payload)
    text=[_block_text(x) for x in blocks]
    expected=["0. 当前状态 / 快速导航","1. 当前初步结论","2. 当前重点问题","3. 证据完整度","4. 最新一次复现结果","5. 多次复现汇总","6. A/B 对比","7. 历次 Reproduction Session（复现会话）","8. 正常项 / 排除性证据","9. 完整技术证据","10. Evidence Bundle / 附件","11. 报告版本与审计记录"]
    positions=[text.index(x) for x in expected]
    assert positions==sorted(positions)
    # Inserting at the current section-11 index places new blocks immediately
    # before section 11, which is exactly the D112 attachment slot.
    assert positions[-2] < attachment_index == positions[-1]
    assert any("首次可观测于 PCM_RX" in x for x in text)
    assert any("5/5" in x and "100.0%" in x for x in text)


def test_feishu_document_projects_offline_call_as_reconstructed_not_reproduction():
    service=FeishuEvidenceDocumentService(transport=_Fake(),storage=_Fake())
    report=SimpleNamespace(id="report-offline",version=1,status="COMPLETE",scope_type="CASE")
    payload={
        "generated_at":"2026-08-20T00:00:00+00:00",
        "case":{"case_no":"CASE-OFFLINE"},
        "completeness":{"state":"COMPLETE","reviewability":"FULLY_REVIEWABLE","capture":{"pcap":True,"pcm_rx":True,"pcm_tx":True}},
        "highest_severity":"HIGH",
        "headline":"发现周期性干扰",
        "evidence_boundary":{"statement":"不确认最终根因。"},
        "findings":[],
        "analysis_context":{
            "analysis_mode":"OFFLINE_IMPORTED",
            "call_origin":"RECONSTRUCTED_FROM_PCAP",
            "call_scope":"BOUND",
            "reconstructed_call_count":1,
        },
        "display_call":{
            "id":"CALL-001",
            "sip_call_id":"00ad1c804c33b255@192.168.3.200",
            "status":"TERMINATED",
            "started_at":"2026-08-14T07:02:49+00:00",
            "ended_at":"2026-08-14T07:03:40+00:00",
            "caller":"8000",
            "dialed_number":"601",
        },
        "call":{
            "id":"CALL-001",
            "sip_call_id":"00ad1c804c33b255@192.168.3.200",
            "status":"TERMINATED",
            "caller":"8000",
            "dialed_number":"601",
        },
        "multi_call_summary":{"call_count":0,"finding_groups":[]},
        "ab_comparison":[],
        "normal_and_exclusion_evidence":[],
        "schema_version":"preliminary-evidence-report-v1",
        "composer_version":"evidence-brief-composer-v1",
    }

    blocks,_=service._core_blocks(report,payload)
    text=[_block_text(x) for x in blocks]

    assert "4. 当前离线 Call 重建结果" in text
    assert "4. 最新一次复现结果" not in text
    assert any("分析方式：离线证据导入" in x and "复现 Session：不适用" in x for x in text)
    assert any("Call：CALL-001" in x and "SIP Call-ID：00ad1c804c33b255@192.168.3.200" in x for x in text)
    assert any("号码：8000 → 601" in x for x in text)
    assert any("当前离线重建 Call 仅用于证据展示，不计入 Reproduction Call 复现次数" in x for x in text)
    assert any("不会为了填充报告创建 ReproductionSession/ReproductionCall" in x for x in text)
    assert not any("Call：None" in x for x in text)

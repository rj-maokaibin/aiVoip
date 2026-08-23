from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from app.golden.offline_analysis_e2e import build_offline_analysis_bundle
from app.integrations.feishu.document_acl import FeishuDocumentPermissionAdapter
from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.integrations.feishu.transport import FeishuLiveTransport

PCAP = Path(os.environ["OFFLINE_PCAP"])
DOC = os.environ["DOCUMENT_ID"]
OPEN_ID = os.environ["TARGET_OPEN_ID"]
HEAD = os.environ["FIXED_MASTER_HEAD"]
OUT = Path("validation/fixed_master_live_refresh")
RESULT = OUT / "result.json"


def block_text(block: dict) -> str:
    for key in ("text", "heading1", "heading2", "heading3", "bullet"):
        body = block.get(key)
        if body:
            return "".join(
                str((item.get("text_run") or {}).get("content") or "")
                for item in body.get("elements") or []
            )
    return ""


async def list_root_children(transport: FeishuLiveTransport) -> list[dict]:
    rows: list[dict] = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        response = await transport._request(
            "GET",
            f"/docx/v1/documents/{quote(DOC, safe='')}/blocks/{quote(DOC, safe='')}/children",
            params=params,
        )
        data = response.get("data") or {}
        rows.extend(data.get("items") or data.get("children") or [])
        if not data.get("has_more"):
            return rows
        page_token = data.get("page_token")
        assert page_token


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = build_offline_analysis_bundle(
        pcap_path=PCAP,
        pcm_profile_path="profiles/pcm/ruijie_aim_diag_v1.yaml",
        output_dir=OUT / "analysis",
        tshark_binary=os.environ["TSHARK_BINARY"],
    )
    assert bundle["source"]["sha256"] == os.environ["OFFLINE_PCAP_SHA256"]

    report = bundle["report"]
    diagnosis = bundle["diagnosis"]
    scope = report.get("problem_scope") or {}
    window = report.get("observation_window") or {}
    actions = report.get("next_actions") or []

    assert diagnosis.get("state") == "DIAGNOSED", diagnosis
    assert len(diagnosis.get("plan") or []) >= 2, diagnosis
    assert scope.get("hypothesis_code") == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", scope
    assert scope.get("affected_path") == "被测设备本地音频采集链路（PCM RX → 上行 RTP）", scope
    assert window.get("scope") == "ACTIVE_MEDIA_WINDOW", window
    assert window.get("absolute_start_utc") == "2026-08-14T07:02:52.055640+00:00", window
    assert window.get("absolute_end_utc") == "2026-08-14T07:03:40.535864+00:00", window
    assert window.get("absolute_start_local") == "2026-08-14T15:02:52.055640+08:00", window
    assert window.get("absolute_end_local") == "2026-08-14T15:03:40.535864+08:00", window
    assert window.get("exact_event_window_known") is False, window
    assert len(actions) >= 2, actions
    assert all(item.get("acceptance_criteria") for item in actions[:2]), actions
    assert "B→A" in actions[0]["acceptance_criteria"], actions[0]

    transport = FeishuLiveTransport()
    service = FeishuEvidenceDocumentService(transport=transport)
    marker = "【修订版｜问题范围/绝对时间/下一步｜master固定版】"
    existing = await list_root_children(transport)
    existing_texts = [block_text(item) for item in existing]

    blocks: list[dict] = []

    def add(text: str, kind: int = 2) -> None:
        blocks.append(service._text(text, kind))

    if marker not in existing_texts:
        add(marker, 3)
        add(
            f"本节由修复后的 master@{HEAD} 基于同一真实PCAP重新分析生成；"
            "下方旧版内容保留作为历史记录，若有冲突以本修订版为准。"
        )
        add("A. 问题范围与绝对时间", 4)
        add(f"诊断结论：{(diagnosis.get('summary') or {}).get('headline')}")
        add(f"问题范围：{scope.get('statement')}")
        add(f"已确认影响链路：{scope.get('affected_path')}")
        for text in (scope.get("excluded_or_weakened") or [])[:8]:
            add(f"已排除/明显弱化：{text}", 12)
        for text in (scope.get("unresolved") or [])[:8]:
            add(f"尚未确认：{text}", 12)
        add(f"观察窗口：{window.get('scope')}")
        add(
            f"绝对时间（UTC）：{window.get('absolute_start_utc')} ～ "
            f"{window.get('absolute_end_utc')}"
        )
        add(
            f"绝对时间（UTC+8）：{window.get('absolute_start_local')} ～ "
            f"{window.get('absolute_end_local')}"
        )
        add(f"精确异常首末时刻已知：否｜{window.get('boundary_statement')}")

        add("B. 下一步建议 / 验证顺序 / 通过标准", 4)
        for index, action in enumerate(actions, 1):
            add(
                f"P{index - 1}｜{action.get('action_type')}｜"
                f"priority={action.get('priority')}｜risk={action.get('risk_level')}",
                5,
            )
            add(f"目的：{action.get('reason')}")
            for step in action.get("execution_steps") or []:
                add(f"执行：{step}", 12)
            add(f"通过标准：{action.get('acceptance_criteria')}")

        add("C. 诊断边界", 4)
        for text in (diagnosis.get("known") or [])[:12]:
            add(f"已知：{text}", 12)
        for text in (diagnosis.get("excluded") or [])[:8]:
            add(f"排除性证据：{text}", 12)
        for text in (diagnosis.get("unknown") or [])[:8]:
            add(f"未知/待闭环：{text}", 12)
        add(f"输入PCAP SHA256：{bundle['source']['sha256']}｜分析代码：master@{HEAD}")
        await service._insert_blocks(DOC, blocks, index=0)

    permission = FeishuDocumentPermissionAdapter(transport=transport)
    collaborators = await permission.list_collaborators(DOC)
    current = next(
        (c for c in collaborators if c.member_type == "openid" and c.member_id == OPEN_ID),
        None,
    )
    if current is None:
        await permission.add_collaborator(DOC, member_type="openid", member_id=OPEN_ID, perm="edit")
    elif current.perm != "edit":
        await permission.update_collaborator(DOC, member_type="openid", member_id=OPEN_ID, perm="edit")

    collaborators = await permission.list_collaborators(DOC)
    verified_acl = next(
        (c for c in collaborators if c.member_type == "openid" and c.member_id == OPEN_ID),
        None,
    )
    assert verified_acl is not None and verified_acl.perm == "edit", collaborators

    final = await list_root_children(transport)
    texts = [block_text(item) for item in final]
    assert marker in texts
    assert any(
        "已确认影响链路：被测设备本地音频采集链路（PCM RX → 上行 RTP）" in text
        for text in texts
    )
    assert any("2026-08-14T15:02:52.055640+08:00" in text for text in texts)
    assert any("下一步建议 / 验证顺序 / 通过标准" in text for text in texts)
    assert any("B→A" in text for text in texts)

    result = {
        "status": "PASS",
        "master_head": HEAD,
        "source_pcap_sha256": bundle["source"]["sha256"],
        "document_id": DOC,
        "document_url": f"https://feishu.cn/docx/{DOC}",
        "editor_permission": verified_acl.perm,
        "acl_verified": True,
        "revision_marker_verified": True,
        "problem_scope": scope,
        "observation_window": window,
        "next_actions": actions,
        "diagnosis_plan_count": len(diagnosis.get("plan") or []),
    }
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

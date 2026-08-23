from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from app.golden.offline_analysis_e2e import build_offline_analysis_bundle
from app.golden.offline_report_v2 import finalize_offline_analysis_bundle_v2
from app.integrations.feishu.document_acl import FeishuDocumentPermissionAdapter
from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.integrations.feishu.transport import FeishuLiveTransport


PCAP = Path(os.environ["OFFLINE_PCAP"])
DOC = os.environ["DOCUMENT_ID"]
OPEN_ID = os.environ["TARGET_OPEN_ID"]
HEAD = os.environ["FIXED_MASTER_HEAD"]
OUT = Path("validation/fixed_master_live_refresh")
RESULT = OUT / "result.json"
FORBIDDEN_TEXT = (
    "范围：未绑定",
    "None ～ None",
    "下一步建议：None",
    "Evidence Card: None",
    "【修订版｜问题范围/绝对时间/下一步｜master固定版】",
    "下方旧版内容保留作为历史记录，若有冲突以本修订版为准",
)
D112 = (
    "0. 当前状态 / 快速导航",
    "1. 当前初步结论",
    "2. 当前重点问题",
    "3. 证据完整度",
    "4. 最新一次复现结果",
    "5. 多次复现汇总",
    "6. A/B 对比",
    "7. 历次 Reproduction Session（复现会话）",
    "8. 正常项 / 排除性证据",
    "9. 完整技术证据",
    "10. Evidence Bundle / 附件",
    "11. 报告版本与审计记录",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


async def delete_all_root_children(transport: FeishuLiveTransport, count: int) -> None:
    if count <= 0:
        return
    await transport._request(
        "DELETE",
        f"/docx/v1/documents/{quote(DOC, safe='')}/blocks/{quote(DOC, safe='')}/children/batch_delete",
        json_body={"start_index": 0, "end_index": count},
    )


def add_bundle_artifacts(report: dict, canonical: dict) -> dict[str, dict]:
    case_id = str((report.get("case") or {}).get("id") or "offline-golden-001")
    created_at = datetime.now(timezone.utc).isoformat()
    bundle = canonical["evidence_bundle"]
    bundle_path = Path(bundle["local_path"])
    manifest_path = Path(bundle["manifest_path"])
    rows = []
    for path, artifact_type, content_type, sha in (
        (bundle_path, "EVIDENCE_BUNDLE", "application/zip", bundle["sha256"]),
        (manifest_path, "MANIFEST_JSON", "application/json", bundle["manifest_sha256"]),
    ):
        artifact_id = f"OFFLINE-{artifact_type}-{sha[:16]}"
        rows.append({
            "artifact_id": artifact_id,
            "type": artifact_type,
            "filename": path.name,
            "content_type": content_type,
            "sha256": sha,
            "size_bytes": path.stat().st_size,
            "local_path": str(path),
            "created_at": created_at,
            "metadata": {
                "case_id": case_id,
                "analyzer_name": "evidence_bundle_builder",
                "analyzer_version": "v1",
                "profile_version": bundle.get("profile"),
                "source_artifact_ids": [],
                "created_at": created_at,
                "offline_materialized": True,
            },
        })
    report.setdefault("artifacts", []).extend(rows)
    return {item["artifact_id"]: item for item in rows}


def report_artifact_index(report: dict) -> dict[str, dict]:
    return {
        str(item.get("artifact_id")): item
        for item in (report.get("artifacts") or [])
        if item.get("artifact_id") and item.get("local_path")
    }


def validate_canonical_report(report: dict) -> None:
    assert report.get("version") == 2, report.get("version")
    assert report.get("report_version") == 2, report.get("report_version")
    assert (report.get("canonical_finalization") or {}).get("finalized") is True
    assert (report.get("projection_contract") or {}).get("single_canonical_fact_layer") is True
    assert (report.get("projection_contract") or {}).get("legacy_prepend_revision_allowed") is False

    diagnosis = report.get("diagnosis") or {}
    assert diagnosis.get("state") == "DIAGNOSED", diagnosis
    assert len(diagnosis.get("plan") or []) >= 2, diagnosis
    scope = report.get("problem_scope") or {}
    window = report.get("observation_window") or {}
    actions = report.get("next_actions") or []
    assert scope.get("hypothesis_code") == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE", scope
    assert scope.get("affected_path") == "被测设备本地音频采集链路（PCM RX → 上行 RTP）", scope
    assert window.get("scope") == "ACTIVE_MEDIA_WINDOW", window
    assert window.get("absolute_start_utc") == "2026-08-14T07:02:52.055640+00:00", window
    assert window.get("absolute_end_utc") == "2026-08-14T07:03:40.535864+00:00", window
    assert window.get("absolute_start_local") == "2026-08-14T15:02:52.055640+08:00", window
    assert window.get("absolute_end_local") == "2026-08-14T15:03:40.535864+08:00", window
    assert window.get("exact_event_window_known") is False, window
    assert len(actions) >= 2, actions
    assert "B→A" in str(actions[0].get("acceptance_criteria") or ""), actions[0]

    completeness = report.get("capture_quality") or {}
    dimensions = completeness.get("dimensions") or {}
    assert tuple(dimensions) == ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION"), dimensions
    assert dimensions["DEBUG"]["requirement"] == "OPTIONAL"
    for name in ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "CORRELATION"):
        assert dimensions[name]["requirement"] == "REQUIRED", (name, dimensions[name])
        assert dimensions[name]["available"] is True, (name, dimensions[name])

    findings = report.get("findings") or []
    assert findings
    periodic = next((item for item in findings if item.get("type") == "LOCAL_CAPTURE_PERIODIC_INTERFERENCE"), None)
    assert periodic is not None, [item.get("type") for item in findings]
    pscope = periodic.get("scope") or {}
    assert pscope.get("pcm_tap") == "pcm_rx", pscope
    assert pscope.get("call_id"), pscope
    # Canonical V2 represents path semantics orthogonally instead of encoding the
    # whole path into one synthetic layer value. Validate the complete tuple so
    # the acceptance gate remains fail-closed while matching the canonical model.
    assert pscope.get("layer") == "pcm_rx", pscope
    assert pscope.get("direction") == "LOCAL_CAPTURE_TO_UPSTREAM_RTP", pscope
    assert pscope.get("path_role") == "LOCAL_CAPTURE_PATH", pscope
    assert pscope.get("upstream_rtp_stream_id"), pscope
    assert pscope.get("downstream_rtp_stream_id"), pscope
    active_window = pscope.get("active_media_window") or {}
    assert active_window.get("start_time") is not None and active_window.get("end_time") is not None, pscope
    representative_window = pscope.get("representative_evidence_window") or {}
    assert representative_window.get("start_time") is not None and representative_window.get("end_time") is not None, pscope
    time_range = periodic.get("time_range") or {}
    assert time_range.get("start") is not None and time_range.get("end") is not None, time_range
    assert time_range.get("exact_event_window_known") is False, time_range
    assert periodic.get("next_action"), periodic
    assert "B→A" in str(periodic.get("verification_acceptance") or ""), periodic
    card = periodic.get("evidence_card") or {}
    assert card, periodic
    assert (card.get("scope") or {}).get("binding_status") == "BOUND", card
    assert (card.get("time") or {}).get("absolute_start_utc"), card
    assert (card.get("time") or {}).get("exact_event_window_known") is False, card
    assert card.get("visual_evidence"), card
    assert (card.get("audio_evidence") or {}).get("status") == "AVAILABLE", card
    visual_types = {item.get("type") for item in card.get("visual_evidence") or []}
    assert "SPECTRUM_PNG" in visual_types, visual_types

    card_summary = report.get("evidence_card_summary") or {}
    assert card_summary.get("finding_count") == len(findings), card_summary
    assert card_summary.get("cards_with_bound_scope") == len(findings), card_summary
    assert card_summary.get("cards_with_bound_time") == len(findings), card_summary
    assert card_summary.get("cards_with_acceptance") == len(findings), card_summary

    provenance = report.get("artifact_provenance_status") or {}
    assert provenance.get("complete") is True, provenance
    serialized = json.dumps(report, ensure_ascii=False)
    for bad in FORBIDDEN_TEXT:
        assert bad not in serialized, bad


def planned_text(blocks: list[dict]) -> str:
    return "\n".join(block_text(item) for item in blocks if block_text(item))


def assert_d112(text: str) -> None:
    offsets = [text.index(title) for title in D112]
    assert offsets == sorted(offsets), offsets


async def upload_local_artifact(
    service: FeishuEvidenceDocumentService,
    *,
    document_id: str,
    created_block: dict,
    artifact: dict,
    image: bool,
) -> bool:
    block_id = service._media_block_id(created_block, image=image)
    path = Path(str(artifact.get("local_path") or ""))
    if not block_id or not path.is_file():
        return False
    data = path.read_bytes()
    token = await service._upload_media(
        block_id=block_id,
        filename=str(artifact.get("filename") or path.name),
        data=data,
        parent_type="docx_image" if image else "docx_file",
    )
    await service._replace_media(document_id, block_id, token, image=image)
    return True


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = build_offline_analysis_bundle(
        pcap_path=PCAP,
        pcm_profile_path="profiles/pcm/ruijie_aim_diag_v1.yaml",
        output_dir=OUT / "analysis",
        tshark_binary=os.environ["TSHARK_BINARY"],
    )
    assert base["source"]["sha256"] == os.environ["OFFLINE_PCAP_SHA256"]
    canonical = finalize_offline_analysis_bundle_v2(
        base,
        source_pcap=PCAP,
        output_dir=OUT / "canonical_v2",
    )
    report = canonical["report"]
    bundle_artifacts = add_bundle_artifacts(report, canonical)

    display_call = report.get("display_call") or report.get("call") or {}
    pseudo = SimpleNamespace(
        id="OFFLINE-CANONICAL-V2",
        case_id=str((report.get("case") or {}).get("id") or "offline-golden-001"),
        session_id=None,
        call_id=display_call.get("id") or display_call.get("call_id"),
        scope_type=(report.get("scope") or {}).get("type") or "CASE",
        scope_id=(report.get("scope") or {}).get("id") or "offline-golden-001",
        version=2,
        status=report.get("status") or "COMPLETE",
    )

    transport = FeishuLiveTransport()
    service = FeishuEvidenceDocumentService(transport=transport)
    existing = await list_root_children(transport)
    legacy_snapshot = OUT / "legacy-feishu-root-before-canonical-v2.json"
    legacy_snapshot.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    legacy_text_snapshot = OUT / "legacy-feishu-text-before-canonical-v2.txt"
    legacy_text_snapshot.write_text("\n".join(block_text(item) for item in existing) + "\n", encoding="utf-8")

    history_blocks = [
        service._text(
            f"历史 V1 投影已在 V2 迁移前保存为审计快照：{legacy_snapshot.name}｜SHA256 {sha256_file(legacy_snapshot)}；"
            "V2 正文不再混排旧结论，避免形成两个事实层。",
            12,
        )
    ]
    core, attachment_index, inline_plan = service._core_blocks(pseudo, report, history_blocks=history_blocks)
    validate_canonical_report(report)
    text_before_write = planned_text(core)
    assert_d112(text_before_write)
    for bad in FORBIDDEN_TEXT:
        assert bad not in text_before_write, bad
    assert "A/B 硬件变量验证" in text_before_write
    assert "多点 PCAP 链路定位" in text_before_write
    assert "B→A" in text_before_write
    assert "Spectrum" in text_before_write or "SPECTRUM" in text_before_write
    assert "Evidence Bundle：已生成" in text_before_write, text_before_write
    assert "Manifest：已生成" in text_before_write, text_before_write

    # All local/report gates above must pass before destructive migration of the
    # living Feishu projection.
    await delete_all_root_children(transport, len(existing))
    created_core = await service._insert_blocks(DOC, core, index=0)

    artifact_index = report_artifact_index(report)
    artifact_index.update(bundle_artifacts)
    uploaded_ids: set[str] = set()
    inline_media_count = 0
    for item in inline_plan:
        raw_index = item.get("block_index")
        index = int(raw_index) if raw_index is not None else -1
        if not 0 <= index < len(created_core):
            continue
        artifact = artifact_index.get(str(item.get("artifact_id")))
        if not artifact:
            continue
        if await upload_local_artifact(
            service,
            document_id=DOC,
            created_block=created_core[index],
            artifact=artifact,
            image=bool(item.get("is_image")),
        ):
            uploaded_ids.add(str(artifact.get("artifact_id")))
            inline_media_count += 1

    # Section 10 attachments: always include the standard Bundle, Manifest, and
    # remaining periodic comparison clips (PCM RX / RTP UP / RTP DOWN).
    extras: list[tuple[dict, bool]] = []
    for artifact in report.get("artifacts") or []:
        artifact_id = str(artifact.get("artifact_id") or "")
        if not artifact_id or artifact_id in uploaded_ids:
            continue
        atype = str(artifact.get("type") or "")
        if atype in {"EVIDENCE_BUNDLE", "MANIFEST_JSON", "PERIODIC_AUDIO_CLIP"}:
            extras.append((artifact, False))
    extras.sort(key=lambda pair: (
        0 if pair[0].get("type") == "EVIDENCE_BUNDLE" else 1 if pair[0].get("type") == "MANIFEST_JSON" else 2,
        str(pair[0].get("filename") or ""),
    ))
    extras = extras[:6]
    created_extras = []
    if extras:
        placeholders = [service._media_placeholder(image=image) for _, image in extras]
        created_extras = await service._insert_blocks(DOC, placeholders, index=attachment_index)
        for (artifact, image), created in zip(extras, created_extras):
            if await upload_local_artifact(
                service,
                document_id=DOC,
                created_block=created,
                artifact=artifact,
                image=image,
            ):
                uploaded_ids.add(str(artifact.get("artifact_id")))

    permission = FeishuDocumentPermissionAdapter(transport=transport)
    collaborators = await permission.list_collaborators(DOC)
    current = next((c for c in collaborators if c.member_type == "openid" and c.member_id == OPEN_ID), None)
    if current is None:
        await permission.add_collaborator(DOC, member_type="openid", member_id=OPEN_ID, perm="edit")
    elif current.perm != "edit":
        await permission.update_collaborator(DOC, member_type="openid", member_id=OPEN_ID, perm="edit")
    collaborators = await permission.list_collaborators(DOC)
    verified_acl = next((c for c in collaborators if c.member_type == "openid" and c.member_id == OPEN_ID), None)
    assert verified_acl is not None and verified_acl.perm == "edit", collaborators

    final = await list_root_children(transport)
    final_text = "\n".join(block_text(item) for item in final if block_text(item))
    assert_d112(final_text)
    for bad in FORBIDDEN_TEXT:
        assert bad not in final_text, bad
    assert "已确认影响链路：被测设备本地音频采集链路（PCM RX → 上行 RTP）" in final_text
    assert "2026-08-14T15:02:52.055640+08:00" in final_text
    assert "A/B 硬件变量验证" in final_text
    assert "多点 PCAP 链路定位" in final_text
    assert "B→A" in final_text
    assert "PCAP：可用｜必需" in final_text
    assert "SIP：可用｜必需" in final_text
    assert "RTP：可用｜必需" in final_text
    assert "PCM_RX：可用｜必需" in final_text
    assert "PCM_TX：可用｜必需" in final_text
    assert "CORRELATION：可用｜必需" in final_text
    assert "DEBUG：缺失/不可用｜可选" in final_text or "DEBUG：可用｜可选" in final_text
    assert "Evidence Bundle：已生成" in final_text
    assert "Manifest：已生成" in final_text

    media_root_count = sum(1 for item in final if item.get("block_type") in {23, 27, 33})
    assert inline_media_count >= 3, inline_media_count
    assert len(uploaded_ids) >= 5, uploaded_ids
    assert media_root_count >= 3, media_root_count

    result = {
        "status": "PASS",
        "master_head": HEAD,
        "source_pcap_sha256": base["source"]["sha256"],
        "document_id": DOC,
        "document_url": f"https://feishu.cn/docx/{DOC}",
        "editor_permission": verified_acl.perm,
        "acl_verified": True,
        "canonical_v2_verified": True,
        "single_canonical_fact_layer_verified": True,
        "d112_order_verified": True,
        "forbidden_legacy_strings_absent": True,
        "finding_scope_time_action_acceptance_verified": True,
        "seven_dimension_completeness_verified": True,
        "root_cause_authority_verified": True,
        "inline_media_count": inline_media_count,
        "uploaded_artifact_count": len(uploaded_ids),
        "root_media_block_count": media_root_count,
        "legacy_projection_snapshot": str(legacy_snapshot),
        "legacy_projection_snapshot_sha256": sha256_file(legacy_snapshot),
        "canonical_report_json": canonical.get("canonical_report_json"),
        "canonical_report_html": canonical.get("canonical_report_html"),
        "evidence_bundle": canonical.get("evidence_bundle"),
        "problem_scope": report.get("problem_scope"),
        "observation_window": report.get("observation_window"),
        "next_actions": report.get("next_actions"),
        "diagnosis_plan_count": len((report.get("diagnosis") or {}).get("plan") or []),
        "capture_quality": report.get("capture_quality"),
        "evidence_card_summary": report.get("evidence_card_summary"),
        "artifact_provenance_status": report.get("artifact_provenance_status"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

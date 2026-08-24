from __future__ import annotations

from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService


def _block_text(block: dict) -> str:
    for key in ("text", "heading1", "heading2", "heading3", "bullet"):
        node = block.get(key)
        if not isinstance(node, dict):
            continue
        parts = []
        for item in node.get("elements") or []:
            run = item.get("text_run") if isinstance(item, dict) else None
            if isinstance(run, dict):
                parts.append(str(run.get("content") or ""))
        return "".join(parts)
    return ""


def _human_visual() -> dict:
    return {
        "artifact_id": "human-spectrum-1",
        "type": "SPECTRUM_PNG",
        "caption": "SPECTRUM｜存在周期性音频干扰。",
        "annotation_contract": {
            "human_explanation_rendered": "STRUCTURED_POST_IMAGE_V2",
            "human_explanation": {
                "what_to_look_at": "查看频率成分及自动标记峰值。",
                "observations": ["检测到约20ms重复周期。", "150/250/350Hz存在梳状峰。"],
                "meaning": "支持数字音频中存在稳定周期性结构。",
                "evidence_boundary": "不能单独确认电源、接地、话柄或SLIC根因。",
                "plain_language_summary": "PCM RX 中存在周期性音频干扰证据。",
                "diagnostic_authority": "NONE",
            },
        },
    }


def test_feishu_human_visual_renders_image_before_five_plain_language_sections():
    service = HumanFeishuEvidenceDocumentService(transport=object(), storage=object())
    blocks: list[dict] = []
    plan: list[dict] = []
    card = {"finding_id": "finding-1", "visual_evidence": [_human_visual()], "audio_evidence": {"status": "NOT_APPLICABLE"}}

    used = service._append_inline_media(blocks, plan, card, budget=3)
    assert used == 1
    image_index = next(i for i, block in enumerate(blocks) if block.get("block_type") == 27)
    texts = [(i, _block_text(block)) for i, block in enumerate(blocks)]
    section_positions = {
        label: next(i for i, text in texts if text.startswith(label))
        for label in ("📖 这张图怎么看", "🔎 图中发现", "💡 这意味着", "⚠️ 证据边界", "✅ 一句话结论")
    }
    assert all(index > image_index for index in section_positions.values())
    assert section_positions["📖 这张图怎么看"] < section_positions["🔎 图中发现"] < section_positions["💡 这意味着"] < section_positions["⚠️ 证据边界"] < section_positions["✅ 一句话结论"]
    assert plan[0]["block_index"] == image_index
    assert plan[0]["human_explanation"] is True


def test_feishu_machine_visual_keeps_compatibility_without_fake_human_explanation():
    service = HumanFeishuEvidenceDocumentService(transport=object(), storage=object())
    blocks: list[dict] = []
    plan: list[dict] = []
    machine = {"artifact_id": "machine-1", "type": "WAVEFORM_PNG", "caption": "Machine waveform", "annotation_contract": {}}
    used = service._append_inline_media(blocks, plan, {"finding_id": "f", "visual_evidence": [machine], "audio_evidence": {}}, budget=1)
    assert used == 1
    assert any(block.get("block_type") == 27 for block in blocks)
    assert not any(_block_text(block).startswith("📖 这张图怎么看") for block in blocks)
    assert plan[0]["human_explanation"] is False

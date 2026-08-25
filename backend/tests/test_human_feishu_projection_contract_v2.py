from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService


def _text_of(block: dict) -> str:
    parts = []

    def walk(value):
        if isinstance(value, dict):
            text_run = value.get("text_run")
            if isinstance(text_run, dict) and text_run.get("content") is not None:
                parts.append(str(text_run.get("content")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(block)
    return "".join(parts)


def _explanation():
    return {
        "what_to_look_at": "先看证据窗口与异常标记。",
        "observations": ["观察事实 A", "观察事实 B"],
        "meaning": "这些事实支持当前 Canonical Finding。",
        "evidence_boundary": "不能仅凭该图确认物理根因。",
        "plain_language_summary": "图中异常与 Finding 一致。",
        "diagnostic_authority": "NONE",
    }


def test_human_feishu_projection_is_image_then_five_explanation_sections():
    service = HumanFeishuEvidenceDocumentService()
    blocks = []
    plan = []
    card = {
        "finding_id": "finding-1",
        "visual_evidence": [{
            "artifact_id": "artifact-1",
            "type": "SPECTRUM_PNG",
            "caption": "代表性证据窗口",
            "annotation_contract": {"human_explanation": _explanation()},
        }],
        "audio_evidence": {},
    }

    used = service._append_inline_media(blocks, plan, card, budget=3)

    assert used == 1
    assert plan == [{
        "block_index": 1,
        "artifact_id": "artifact-1",
        "is_image": True,
        "finding_id": "finding-1",
        "human_explanation": True,
    }]
    assert blocks[1]["block_type"] == 27

    texts = [_text_of(block) for block in blocks[2:]]
    labels = [
        "📖 这张图怎么看：",
        "🔎 图中发现：",
        "💡 这意味着：",
        "⚠️ 证据边界：",
        "✅ 一句话结论：",
    ]
    positions = []
    for label in labels:
        positions.append(next(i for i, text in enumerate(texts) if label in text))
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_incomplete_human_explanation_does_not_emit_five_section_body():
    service = HumanFeishuEvidenceDocumentService()
    blocks = []
    plan = []
    card = {
        "finding_id": "finding-2",
        "visual_evidence": [{
            "artifact_id": "artifact-2",
            "type": "SPECTRUM_PNG",
            "caption": "fallback visual",
            "annotation_contract": {"human_explanation": {"what_to_look_at": "only one field"}},
        }],
        "audio_evidence": {},
    }

    service._append_inline_media(blocks, plan, card, budget=3)
    text = "\n".join(_text_of(block) for block in blocks)
    assert "📖 这张图怎么看：" not in text
    assert "✅ 一句话结论：" not in text
    assert plan[0]["human_explanation"] is False

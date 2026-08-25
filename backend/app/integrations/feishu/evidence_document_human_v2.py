from __future__ import annotations

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService


class HumanFeishuEvidenceDocumentService(FeishuEvidenceDocumentService):
    """Human V2 projection: image first, then structured plain-language explanation.

    Canonical report/finding data is unchanged; this class changes presentation
    order only. Machine visuals and audio retain the existing compatibility path.
    """

    LIVING_PROJECTION_CONTRACT = "feishu-evidence-living-document-human-v2"

    @staticmethod
    def _human_explanation(artifact: dict) -> dict | None:
        annotation = artifact.get("annotation_contract") or {}
        value = annotation.get("human_explanation")
        if not isinstance(value, dict):
            return None
        required = ("what_to_look_at", "meaning", "evidence_boundary", "plain_language_summary")
        if any(not str(value.get(key) or "").strip() for key in required):
            return None
        if str(value.get("diagnostic_authority") or "NONE").upper() != "NONE":
            return None
        return value

    def _append_human_explanation(self, blocks: list[dict], explanation: dict) -> None:
        blocks.append(self._text(f"📖 这张图怎么看：{explanation.get('what_to_look_at')}", 12))
        observations = [str(x).strip() for x in (explanation.get("observations") or []) if str(x).strip()]
        if observations:
            blocks.append(self._text("🔎 图中发现：", 12))
            for item in observations[:8]:
                blocks.append(self._text(item, 12))
        else:
            blocks.append(self._text("🔎 图中发现：当前没有额外 Human Measurement；请以 Canonical Finding 测量和原始 Evidence 为准。", 12))
        blocks.append(self._text(f"💡 这意味着：{explanation.get('meaning')}", 12))
        blocks.append(self._text(f"⚠️ 证据边界：{explanation.get('evidence_boundary')}", 12))
        blocks.append(self._text(f"✅ 一句话结论：{explanation.get('plain_language_summary')}", 12))

    def _append_inline_media(self, blocks: list[dict], plan: list[dict], card: dict, *, budget: int) -> int:
        used = 0
        chosen: list[tuple[dict, bool]] = []
        for visual in (card.get("visual_evidence") or [])[:3]:
            chosen.append((visual, True))
        audio = card.get("audio_evidence") or {}
        if audio.get("status") == "AVAILABLE" and audio.get("clips"):
            chosen.append((audio["clips"][0], False))
        elif audio.get("status") == "UNAVAILABLE":
            blocks.append(self._text(f"⚠️ 异常音频：暂不可用｜{audio.get('reason')}", 12))

        for artifact, is_image in chosen:
            if used >= budget:
                break
            artifact_id = artifact.get("artifact_id")
            if not artifact_id:
                continue
            explanation = self._human_explanation(artifact) if is_image else None
            label = "证据图" if is_image else "证据附件"
            blocks.append(self._text(f"{label}：{artifact.get('type')}｜{artifact.get('caption')}", 12))
            block_index = len(blocks)
            blocks.append(self._media_placeholder(image=is_image))
            plan.append({
                "block_index": block_index,
                "artifact_id": artifact_id,
                "is_image": is_image,
                "finding_id": card.get("finding_id"),
                "human_explanation": bool(explanation),
            })
            if explanation:
                self._append_human_explanation(blocks, explanation)
            used += 1
        return used

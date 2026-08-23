import pytest

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService


class FakeTransport:
    def __init__(self):
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"code": 0, "data": {}}


@pytest.mark.asyncio
async def test_living_document_deletes_only_exact_tracked_root_range():
    transport = FakeTransport()
    service = FeishuEvidenceDocumentService(transport=transport, storage=object())
    service.DOC_EDIT_INTERVAL_SECONDS = 0

    await service._delete_tracked_projection("doc-1", 7)

    assert transport.calls == [(
        "DELETE",
        "/docx/v1/documents/doc-1/blocks/doc-1/children/batch_delete",
        {"json_body": {"start_index": 0, "end_index": 7}},
    )]


@pytest.mark.asyncio
async def test_living_document_never_guesses_legacy_untracked_range():
    transport = FakeTransport()
    service = FeishuEvidenceDocumentService(transport=transport, storage=object())
    service.DOC_EDIT_INTERVAL_SECONDS = 0

    await service._delete_tracked_projection("doc-1", 0)

    assert transport.calls == []


def _block_text(block: dict) -> str:
    for key in ("text", "heading1", "heading2", "heading3", "bullet"):
        body = block.get(key)
        if body:
            return "".join(
                str((element.get("text_run") or {}).get("content") or "")
                for element in body.get("elements") or []
            )
    return ""


def test_feishu_section_3_renders_frozen_seven_dimension_completeness():
    service = FeishuEvidenceDocumentService(transport=FakeTransport(), storage=object())
    report = type("Report", (), {
        "version": 2,
        "status": "PARTIAL_COMPLETE",
        "scope_type": "CALL",
        "id": "REPORT-2",
    })()
    dimensions = {
        name: {"available": name != "DEBUG"}
        for name in ("PCAP", "SIP", "RTP", "PCM_RX", "PCM_TX", "DEBUG", "CORRELATION")
    }
    payload = {
        "generated_at": "2026-08-22T00:00:00Z",
        "case": {"case_no": "CASE-001"},
        "completeness": {
            "state": "PARTIAL",
            "frozen_v1": {"state": "PARTIAL", "dimensions": dimensions},
        },
        "findings": [],
        "evidence_boundary": {"statement": "UNKNOWN where evidence is missing"},
        "analysis_context": {"analysis_mode": "OFFLINE_IMPORTED"},
        "normal_evidence": [],
    }

    blocks, _, _ = service._core_blocks(report, payload)
    texts = [_block_text(block) for block in blocks]

    for name in dimensions:
        assert any(name in text for text in texts)
    assert any("DEBUG" in text and "缺失/不可用" in text for text in texts)
    assert service.LIVING_PROJECTION_CONTRACT == "feishu-evidence-living-document-v2"

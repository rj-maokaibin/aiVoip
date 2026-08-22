from types import SimpleNamespace

import pytest

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService


class _Storage:
    def __init__(self):
        self.objects = {"img": b"png", "wav": b"wav"}

    def get_bytes(self, key):
        return self.objects[key]


def test_media_block_id_resolves_image_and_file_view_wrapper():
    assert FeishuEvidenceDocumentService._media_block_id(
        {"block_id": "image-block", "block_type": 27}, image=True
    ) == "image-block"
    assert FeishuEvidenceDocumentService._media_block_id(
        {"block_id": "view-block", "block_type": 33, "children": ["file-block"]}, image=False
    ) == "file-block"
    assert FeishuEvidenceDocumentService._media_block_id(
        {"block_id": "file-block", "block_type": 23}, image=False
    ) == "file-block"


@pytest.mark.asyncio
async def test_materialize_plan_targets_real_media_blocks_and_replaces_tokens(monkeypatch):
    service = FeishuEvidenceDocumentService(storage=_Storage())
    uploads = []
    replacements = []

    async def fake_upload_media(*, block_id, filename, data, parent_type):
        uploads.append((block_id, filename, data, parent_type))
        return f"token-{block_id}"

    async def fake_replace_media(document_id, block_id, token, *, image):
        replacements.append((document_id, block_id, token, image))

    monkeypatch.setattr(service, "_upload_media", fake_upload_media)
    monkeypatch.setattr(service, "_replace_media", fake_replace_media)

    artifacts = {
        "image-artifact": SimpleNamespace(
            id="image-artifact", object_key="img", filename="waveform.png", size_bytes=3
        ),
        "audio-artifact": SimpleNamespace(
            id="audio-artifact", object_key="wav", filename="clip.wav", size_bytes=3
        ),
    }
    created = [
        {"block_id": "image-block", "block_type": 27},
        {"block_id": "view-block", "block_type": 33, "children": ["file-block"]},
    ]
    plan = [
        {"block_index": 0, "artifact_id": "image-artifact", "is_image": True},
        {"block_index": 1, "artifact_id": "audio-artifact", "is_image": False},
    ]

    used = await service._materialize_plan("doc-id", created, plan, artifacts)

    assert used == {"image-artifact", "audio-artifact"}
    assert uploads == [
        ("image-block", "waveform.png", b"png", "docx_image"),
        ("file-block", "clip.wav", b"wav", "docx_file"),
    ]
    assert replacements == [
        ("doc-id", "image-block", "token-image-block", True),
        ("doc-id", "file-block", "token-file-block", False),
    ]

from types import SimpleNamespace

import pytest

from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService


class _Storage:
    def __init__(self):
        self.objects = {"img": b"png", "wav": b"wav"}

    def get_bytes(self, key):
        return self.objects[key]


@pytest.mark.asyncio
async def test_materialize_plan_uploads_media_to_placeholder_without_second_patch(monkeypatch):
    service = FeishuEvidenceDocumentService(storage=_Storage())
    calls = []

    async def fake_upload_media(*, block_id, filename, data, parent_type):
        calls.append((block_id, filename, data, parent_type))
        return "media-token"

    async def forbidden_replace(*args, **kwargs):
        raise AssertionError("docx media upload already associates the token with the media block")

    monkeypatch.setattr(service, "_upload_media", fake_upload_media)
    monkeypatch.setattr(service, "_replace_media", forbidden_replace)

    artifacts = {
        "image-artifact": SimpleNamespace(
            id="image-artifact", object_key="img", filename="waveform.png", size_bytes=3
        ),
        "audio-artifact": SimpleNamespace(
            id="audio-artifact", object_key="wav", filename="clip.wav", size_bytes=3
        ),
    }
    created = [{"block_id": "image-block"}, {"block_id": "file-block"}]
    plan = [
        {"block_index": 0, "artifact_id": "image-artifact", "is_image": True},
        {"block_index": 1, "artifact_id": "audio-artifact", "is_image": False},
    ]

    used = await service._materialize_plan("doc-id", created, plan, artifacts)

    assert used == {"image-artifact", "audio-artifact"}
    assert calls == [
        ("image-block", "waveform.png", b"png", "docx_image"),
        ("file-block", "clip.wav", b"wav", "docx_file"),
    ]

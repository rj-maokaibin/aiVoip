from types import SimpleNamespace

from app.services.evidence_report_artifacts import _artifact_allowed_for_profile, _artifact_bundle_path


def _artifact(kind:str,content_type:str="application/octet-stream"):
    return SimpleNamespace(id="12345678-aaaa",type=kind,filename=f"{kind.lower()}.dat",content_type=content_type)


def test_share_safe_excludes_full_audio_but_keeps_abnormal_clips_and_images():
    assert _artifact_allowed_for_profile(_artifact("PCM_WAV"),"SHARE_SAFE") is False
    assert _artifact_allowed_for_profile(_artifact("AUDIO_WAV"),"SHARE_SAFE") is False
    assert _artifact_allowed_for_profile(_artifact("RTP_WAV"),"SHARE_SAFE") is False
    assert _artifact_allowed_for_profile(_artifact("AUDIO_CLIP"),"SHARE_SAFE") is True
    assert _artifact_allowed_for_profile(_artifact("SPECTROGRAM_PNG","image/png"),"SHARE_SAFE") is True


def test_internal_full_keeps_full_audio_and_directory_schema_is_stable():
    pcm=_artifact("PCM_WAV")
    clip=_artifact("AUDIO_CLIP")
    image=_artifact("SPECTRUM_PNG","image/png")
    assert _artifact_allowed_for_profile(pcm,"INTERNAL_FULL") is True
    assert _artifact_bundle_path(pcm).startswith("audio/full/")
    assert _artifact_bundle_path(clip).startswith("audio/clips/")
    assert _artifact_bundle_path(image).startswith("images/")

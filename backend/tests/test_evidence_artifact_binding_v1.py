from __future__ import annotations

from types import SimpleNamespace

from app.services.evidence_report_source_artifacts import artifact_matches_finding


def _artifact(atype:str,meta:dict):
    return SimpleNamespace(type=atype,metadata_json=meta)


def _finding(ftype:str,*,start:float=100.0,end:float|None=None,tap:str|None=None,session:int|None=None,stream:str|None=None):
    scope={}
    if tap is not None:scope["pcm_tap"]=tap
    if session is not None:scope["pcm_session_index"]=session
    if stream is not None:scope["rtp_stream_id"]=stream
    return SimpleNamespace(
        finding_type=ftype,
        scope_json=scope,
        start_time=start,
        end_time=end if end is not None else start,
        representative_time=start,
    )


def test_same_pcm_tap_but_different_event_type_must_not_attach_audio_clip():
    click=_artifact("AUDIO_CLIP",{"pcm_tap":"pcm_rx","session_index":0,"event_type":"CLICK_POP","event_time":100.0})
    silence=_finding("UNEXPECTED_SILENCE",start=100.0,tap="pcm_rx",session=0)

    assert artifact_matches_finding(click,silence) is False


def test_same_pcm_tap_and_type_but_different_time_must_not_attach_audio_clip():
    click=_artifact("AUDIO_CLIP",{"pcm_tap":"pcm_rx","session_index":0,"event_type":"CLICK_POP","event_time":110.0})
    finding=_finding("CLICK_POP",start=100.0,tap="pcm_rx",session=0)

    assert artifact_matches_finding(click,finding) is False


def test_silence_alias_clip_matches_unexpected_silence_when_time_and_scope_agree():
    clip=_artifact("AUDIO_CLIP",{"pcm_tap":"pcm_tx","session_index":2,"event_type":"SILENCE","event_time":100.12})
    finding=_finding("UNEXPECTED_SILENCE",start=100.0,end=100.4,tap="pcm_tx",session=2)

    assert artifact_matches_finding(clip,finding) is True


def test_rtp_high_delta_clip_requires_matching_stream_and_event_time():
    clip=_artifact("AUDIO_CLIP",{"stream_id":"rtp-up","event_type":"HIGH_DELTA","event_time":100.2})
    right=_finding("HIGH_DELTA",start=100.2,stream="rtp-up")
    wrong_stream=_finding("HIGH_DELTA",start=100.2,stream="rtp-down")

    assert artifact_matches_finding(clip,right) is True
    assert artifact_matches_finding(clip,wrong_stream) is False


def test_periodic_nested_scope_clip_matches_local_capture_periodic_finding():
    clip=_artifact("PERIODIC_AUDIO_CLIP",{
        "event_type":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
        "source":"pcm_rx",
        "scope":{"pcm_tap":"pcm_rx","pcm_session_index":0,"upstream_rtp_stream_id":"rtp-up"},
    })
    finding=_finding("LOCAL_CAPTURE_PERIODIC_INTERFERENCE",start=100.0,tap="pcm_rx",session=0)
    finding.scope_json["upstream_rtp_stream_id"]="rtp-up"

    assert artifact_matches_finding(clip,finding) is True


def test_candidate_audio_is_not_part_of_report_source_binding_contract():
    candidate=_artifact("CANDIDATE_AUDIO_CLIP",{"pcm_tap":"pcm_rx","session_index":0,"event_type":"CLICK_POP","event_time":100.0})
    finding=_finding("CLICK_POP",start=100.0,tap="pcm_rx",session=0)

    assert artifact_matches_finding(candidate,finding) is False

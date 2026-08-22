from __future__ import annotations

from app.services.evidence_report_context import (
    CALL_BINDING_INCOMPLETE,
    FULLY_REVIEWABLE,
    NOT_FULLY_REVIEWABLE,
    resolve_report_analysis_context,
)
from app.services.evidence_report_subject_call import infer_pcm_source_device_identity


DUT_IP = "192.168.150.4"
PBX_IP = "192.168.3.200"
PEER_IP = "192.168.150.8"
DUT_CALL_ID = "00ad1c804c33b255@192.168.3.200"
PBX_LEG_CALL_ID = "pbx-b2bua-leg@192.168.3.200"


def _call(call_id: str, start: float, end: float, offer_ip: str, answer_ip: str, stream_ids: list[str]) -> dict:
    return {
        "call_id": call_id,
        "caller": "sip:8000@192.168.3.200",
        "callee": "sip:601@192.168.3.200",
        "state": "TERMINATED",
        "start_time": start,
        "end_time": end,
        "media_start_time": start + 1.0,
        "media_end_time": end - 1.0,
        "invite_final_status": 200,
        "rtp_stream_ids": stream_ids,
        "capture_completeness": {"is_partial": False},
        "media_direction_health": {"status": "BIDIRECTIONAL"},
        "sdp": {
            "offer": {"connection_address": offer_ip, "media": [{"media": "audio", "connection_address": offer_ip, "port": 10000}]},
            "answer": {"connection_address": answer_ip, "media": [{"media": "audio", "connection_address": answer_ip, "port": 11446}]},
        },
        "ladder": [],
    }


def _packet() -> dict:
    dut_up = f"{DUT_IP}:10000>{PBX_IP}:11446/ssrc=1"
    dut_down = f"{PBX_IP}:11446>{DUT_IP}:10000/ssrc=2"
    pbx_up = f"{PBX_IP}:11452>{PEER_IP}:10000/ssrc=1"
    peer_down = f"{PEER_IP}:10000>{PBX_IP}:11452/ssrc=3"
    calls = [
        _call(DUT_CALL_ID, 100.0, 151.0, DUT_IP, PBX_IP, [dut_up, dut_down]),
        # This internal B2BUA leg ends later. A legacy latest-call selector would
        # incorrectly choose it even though it does not contain the PCM source DUT.
        _call(PBX_LEG_CALL_ID, 100.1, 151.2, PBX_IP, PEER_IP, [pbx_up, peer_down]),
    ]
    streams = [
        {"stream_id": dut_up, "src_ip": DUT_IP, "src_port": 10000, "dst_ip": PBX_IP, "dst_port": 11446},
        {"stream_id": dut_down, "src_ip": PBX_IP, "src_port": 11446, "dst_ip": DUT_IP, "dst_port": 10000},
        {"stream_id": pbx_up, "src_ip": PBX_IP, "src_port": 11452, "dst_ip": PEER_IP, "dst_port": 10000},
        {"stream_id": peer_down, "src_ip": PEER_IP, "src_port": 10000, "dst_ip": PBX_IP, "dst_port": 11452},
    ]
    return {"summary": {"call_count": 2, "rtp_stream_count": 4}, "calls": calls, "rtp_streams": streams, "anomalies": []}


def _pcm(source_ip: str = DUT_IP) -> dict:
    return {
        "summary": {"total_packets": 13050},
        "streams": [
            {"tap": {"name": "pcm_rx", "direction": "RX"}, "packet_count": 6525, "source_endpoints": [{"ip": source_ip, "port": 48741, "packet_count": 6525}], "sessions": []},
            {"tap": {"name": "pcm_tx", "direction": "TX"}, "packet_count": 6525, "source_endpoints": [{"ip": source_ip, "port": 46812, "packet_count": 6525}], "sessions": []},
        ],
    }


def test_pcm_source_identity_selects_dut_leg_even_when_pbx_leg_ends_later():
    results = {"packet_intelligence": _packet(), "pcm_intelligence": _pcm(), "media_intelligence": None}
    resolved = resolve_report_analysis_context(
        scope_type="CASE",
        session=None,
        runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}],
        results=results,
    )
    context = resolved["analysis_context"]
    display_call = resolved["display_call"]

    assert context["raw_sip_leg_count"] == 2
    assert context["reconstructed_call_count"] == 2
    assert context["diagnostic_call_count"] == 1
    assert context["subject_device_ip"] == DUT_IP
    assert context["call_selection_status"] == "SELECTED"
    assert context["selection_rule"] == "PCM_SOURCE_DEVICE_IDENTITY_MATCH"
    assert context["selected_sip_call_id"] == DUT_CALL_ID
    assert context["semantic_issues"] == []
    assert context["reviewability"] == FULLY_REVIEWABLE
    assert display_call["sip_call_id"] == DUT_CALL_ID
    assert display_call["id"] == "CALL-001"


def test_multileg_without_subject_identity_can_display_latest_only_as_unverified_fallback():
    results = {"packet_intelligence": _packet(), "pcm_intelligence": None, "media_intelligence": None}
    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}], results=results,
    )
    context = resolved["analysis_context"]

    assert resolved["display_call"]["sip_call_id"] == PBX_LEG_CALL_ID
    assert context["call_selection_status"] == "AMBIGUOUS"
    assert context["selection_rule"] == "LATEST_RECONSTRUCTED_CALL_BY_END_THEN_START_TIME"
    assert CALL_BINDING_INCOMPLETE in context["semantic_issues"]
    assert context["reviewability"] == NOT_FULLY_REVIEWABLE


def test_multiple_pcm_source_devices_do_not_create_false_subject_authority():
    pcm = _pcm()
    pcm["streams"][1]["source_endpoints"] = [{"ip": "192.168.150.99", "port": 46812, "packet_count": 6525}]
    identity = infer_pcm_source_device_identity({"pcm_intelligence": pcm, "media_intelligence": None})
    assert identity["status"] == "AMBIGUOUS"
    assert identity["selected_ip"] is None

    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}],
        results={"packet_intelligence": _packet(), "pcm_intelligence": pcm, "media_intelligence": None},
    )
    assert resolved["analysis_context"]["call_selection_status"] == "AMBIGUOUS"
    assert resolved["analysis_context"]["reviewability"] == NOT_FULLY_REVIEWABLE


def test_single_reconstructed_call_remains_reviewable_without_pcm_identity():
    packet = _packet()
    packet["calls"] = [packet["calls"][0]]
    packet["summary"]["call_count"] = 1
    resolved = resolve_report_analysis_context(
        scope_type="CASE", session=None, runtime_call=None,
        evidences=[{"type": "PCAP", "source": "USER_UPLOAD"}],
        results={"packet_intelligence": packet, "pcm_intelligence": None, "media_intelligence": None},
    )
    assert resolved["display_call"]["sip_call_id"] == DUT_CALL_ID
    assert resolved["analysis_context"]["selection_rule"] == "ONLY_RECONSTRUCTED_CALL"
    assert resolved["analysis_context"]["reviewability"] == FULLY_REVIEWABLE

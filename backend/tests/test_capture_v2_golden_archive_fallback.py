from app.capture_v2.gate.golden_archive_fallback import _decode_sip


def test_decode_sip_invite_extracts_call_id_target_and_sdp_audio_port():
    payload = (
        b"INVITE sip:601@192.168.3.200 SIP/2.0\r\n"
        b"Via: SIP/2.0/UDP 192.168.150.10:5060\r\n"
        b"Call-ID: golden-call-1\r\n"
        b"CSeq: 1 INVITE\r\n"
        b"Content-Type: application/sdp\r\n"
        b"Content-Length: 80\r\n\r\n"
        b"v=0\r\nc=IN IP4 192.168.150.10\r\nm=audio 18000 RTP/AVP 0 101\r\n"
    )
    parsed = _decode_sip(payload)
    assert parsed is not None
    assert parsed["method"] == "INVITE"
    assert parsed["request_target"] == "sip:601@192.168.3.200"
    assert parsed["call_id"] == "golden-call-1"
    assert parsed["cseq_method"] == "INVITE"
    assert parsed["sdp_audio_ports"] == [18000]


def test_decode_sip_info_extracts_dtmf_signal():
    payload = (
        b"INFO sip:601@192.168.3.200 SIP/2.0\r\n"
        b"Call-ID: golden-call-1\r\n"
        b"CSeq: 9 INFO\r\n"
        b"Content-Type: application/dtmf-relay\r\n\r\n"
        b"Signal=#\r\nDuration=160\r\n"
    )
    parsed = _decode_sip(payload)
    assert parsed is not None
    assert parsed["method"] == "INFO"
    assert parsed["info_signals"] == ["#"]


def test_decode_sip_response_extracts_status_and_compact_call_id():
    payload = (
        b"SIP/2.0 200 OK\r\n"
        b"i: compact-call-id\r\n"
        b"CSeq: 1 INVITE\r\n\r\n"
    )
    parsed = _decode_sip(payload)
    assert parsed is not None
    assert parsed["status_code"] == 200
    assert parsed["call_id"] == "compact-call-id"
    assert parsed["cseq_method"] == "INVITE"


def test_decode_sip_rejects_arbitrary_udp_payload():
    assert _decode_sip(b"not sip at all") is None

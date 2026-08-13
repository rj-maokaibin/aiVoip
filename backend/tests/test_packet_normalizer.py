from app.analyzers.packet.normalize import normalize_ek_record


def test_normalize_tshark_ek_sip_rtp_fields():
    record = {
        "layers": {
            "frame": {"frame_frame_number": "12", "frame_frame_time_epoch": "100.25"},
            "ip": {"ip_ip_src": "10.0.0.1", "ip_ip_dst": "10.0.0.2"},
            "udp": {"udp_udp_srcport": "5060", "udp_udp_dstport": "5060"},
            "sip": {
                "sip_sip_request_line": "INVITE sip:1002@pbx SIP/2.0",
                "sip_sip_method": "INVITE",
                "sip_sip_call_id": "abc",
                "sip_sip_from_addr": "sip:1001@pbx",
                "sip_sip_to_addr": "sip:1002@pbx",
                "sip_sip_cseq_seq": "1",
                "sip_sip_cseq_method": "INVITE",
            },
        }
    }
    pkt = normalize_ek_record(record)
    assert pkt.frame_number == 12
    assert pkt.sip.call_id == "abc"
    assert pkt.sip.request_uri == "sip:1002@pbx"

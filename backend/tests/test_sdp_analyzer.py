from app.analyzers.packet.sdp import parse_sdp_text, negotiate_codecs


def test_sdp_offer_answer_and_ptime():
    offer = parse_sdp_text("""v=0\r\nc=IN IP4 10.0.0.1\r\nm=audio 4000 RTP/AVP 8 0 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=ptime:20\r\na=sendrecv\r\n""")
    answer = parse_sdp_text("""v=0\r\nc=IN IP4 10.0.0.2\r\nm=audio 5000 RTP/AVP 8 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:101 telephone-event/8000\r\na=ptime:20\r\n""")
    assert offer.media[0].connection_address == "10.0.0.1"
    assert offer.media[0].ptime_ms == 20
    assert offer.media[0].telephone_event_payloads == [101]
    negotiated = negotiate_codecs(offer, answer)
    assert [x["name"] for x in negotiated] == ["PCMA"]

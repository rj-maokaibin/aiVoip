from app.services.evidence_report import _visual_source_results


def test_media_only_results_feed_visual_packet_and_pcm_sources():
    media_packet = {"rtp_streams": [{"stream_id": "s1"}]}
    media_pcm = {"streams": [{"tap": {"name": "pcm_rx"}}]}
    results = {
        "packet_intelligence": None,
        "pcm_intelligence": None,
        "media_intelligence": {
            "packet": media_packet,
            "pcm": media_pcm,
            "cross_layer_events": [],
        },
    }

    resolved = _visual_source_results(results)

    assert resolved["packet_intelligence"] is media_packet
    assert resolved["pcm_intelligence"] is media_pcm
    assert results["packet_intelligence"] is None
    assert results["pcm_intelligence"] is None


def test_standalone_packet_and_pcm_remain_authoritative_for_visuals():
    standalone_packet = {"rtp_streams": [{"stream_id": "standalone"}]}
    standalone_pcm = {"streams": [{"tap": {"name": "pcm_tx"}}]}
    results = {
        "packet_intelligence": standalone_packet,
        "pcm_intelligence": standalone_pcm,
        "media_intelligence": {
            "packet": {"rtp_streams": [{"stream_id": "media"}]},
            "pcm": {"streams": [{"tap": {"name": "pcm_rx"}}]},
        },
    }

    resolved = _visual_source_results(results)

    assert resolved["packet_intelligence"] is standalone_packet
    assert resolved["pcm_intelligence"] is standalone_pcm


def test_missing_media_projection_stays_unavailable():
    results = {
        "packet_intelligence": None,
        "pcm_intelligence": None,
        "media_intelligence": {"cross_layer_events": []},
    }

    resolved = _visual_source_results(results)

    assert resolved["packet_intelligence"] is None
    assert resolved["pcm_intelligence"] is None

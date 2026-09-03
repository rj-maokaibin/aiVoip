from app.reports.v2.semantic_validator import validate_foundation_semantics


def _rules(result):
    return {item["rule"] for item in result["violations"]}


def test_foundation_validator_accepts_observed_rtp_window_and_unterminated_call():
    result = validate_foundation_semantics(
        call={"call_end_time": None, "termination": {"observed": False}},
        timeline={
            "media_observation_window": {
                "start": 10.0,
                "end": 20.0,
                "source": "RTP_OBSERVATION",
            }
        },
        rtp_streams=[{"packet_count": 10}],
    )

    assert result["status"] == "PASS"
    assert result["violations"] == []


def test_r001_blocks_synthetic_call_end_without_termination():
    result = validate_foundation_semantics(
        call={"call_end_time": 12.0, "termination": {"observed": False}},
        timeline={"media_observation_window": {}},
        rtp_streams=[],
    )

    assert result["status"] == "FAIL"
    assert "R001" in _rules(result)


def test_r002_r003_block_zero_length_ack_sourced_media_window_when_rtp_exists():
    result = validate_foundation_semantics(
        call={"call_end_time": None, "termination": {"observed": False}},
        timeline={
            "media_observation_window": {
                "start": 12.0,
                "end": 12.0,
                "source": "SIP_ACK",
            }
        },
        rtp_streams=[{"packet_count": 20}],
    )

    assert result["status"] == "FAIL"
    assert _rules(result) == {"R002", "R003"}

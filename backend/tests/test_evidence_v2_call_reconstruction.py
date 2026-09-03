from app.reports.v2.call_reconstruction import reconstruct_call_v2


def _base_call(ladder):
    return {
        "call_id": "call-1",
        "caller": "601",
        "callee": "101",
        "state": "ESTABLISHED",
        "start_time": 1.0,
        "end_time": ladder[-1]["timestamp"],
        "media_start_time": ladder[-1]["timestamp"],
        "media_end_time": ladder[-1]["timestamp"],
        "ladder": ladder,
    }


def test_ack_establishes_call_but_never_terminates_it():
    call = _base_call([
        {"frame_number": 1, "timestamp": 1.0, "method": "INVITE", "status_code": None, "cseq_method": "INVITE"},
        {"frame_number": 2, "timestamp": 1.2, "method": None, "status_code": 180, "cseq_method": "INVITE"},
        {"frame_number": 3, "timestamp": 2.0, "method": None, "status_code": 200, "cseq_method": "INVITE"},
        {"frame_number": 4, "timestamp": 2.01, "method": "ACK", "status_code": None, "cseq_method": "ACK"},
    ])

    result = reconstruct_call_v2(call)

    assert result["state"] == "ESTABLISHED"
    assert result["answer_time"] == 2.0
    assert result["ack_time"] == 2.01
    assert result["established_time"] == 2.01
    assert result["termination"]["observed"] is False
    assert result["call_end_time"] is None
    assert result["duration_seconds"] is None
    assert result["capture_last_signaling_time"] == 2.01


def test_bye_is_an_observed_termination_event():
    call = _base_call([
        {"frame_number": 1, "timestamp": 1.0, "method": "INVITE", "status_code": None, "cseq_method": "INVITE"},
        {"frame_number": 2, "timestamp": 2.0, "method": None, "status_code": 200, "cseq_method": "INVITE"},
        {"frame_number": 3, "timestamp": 2.01, "method": "ACK", "status_code": None, "cseq_method": "ACK"},
        {"frame_number": 4, "timestamp": 12.0, "method": "BYE", "status_code": None, "cseq_method": "BYE"},
    ])

    result = reconstruct_call_v2(call)

    assert result["state"] == "TERMINATED"
    assert result["termination"] == {
        "observed": True,
        "kind": "BYE",
        "time": 12.0,
        "frame_number": 4,
        "status_code": None,
    }
    assert result["call_end_time"] == 12.0
    assert result["duration_seconds"] == 11.0


def test_final_failure_terminates_unestablished_invite():
    call = _base_call([
        {"frame_number": 1, "timestamp": 1.0, "method": "INVITE", "status_code": None, "cseq_method": "INVITE"},
        {"frame_number": 2, "timestamp": 1.1, "method": None, "status_code": 100, "cseq_method": "INVITE"},
        {"frame_number": 3, "timestamp": 1.5, "method": None, "status_code": 486, "cseq_method": "INVITE"},
    ])

    result = reconstruct_call_v2(call)

    assert result["state"] == "FAILED"
    assert result["termination"]["observed"] is True
    assert result["termination"]["kind"] == "FINAL_RESPONSE"
    assert result["termination"]["status_code"] == 486
    assert result["call_end_time"] == 1.5

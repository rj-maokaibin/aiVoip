import pytest

from app.capture_v2.enums import CaptureSessionState, GapCertainty, RecoveryClassification


def test_enum_string_round_trip_and_unknown_fail_fast():
    assert CaptureSessionState(CaptureSessionState.WATCHING.value) is CaptureSessionState.WATCHING
    assert GapCertainty("POSSIBLE") is GapCertainty.POSSIBLE
    assert RecoveryClassification("MULTIPLE_PRODUCERS") is RecoveryClassification.MULTIPLE_PRODUCERS
    with pytest.raises(ValueError):
        CaptureSessionState("NOT_A_STATE")

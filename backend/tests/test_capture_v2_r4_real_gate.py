from app.capture_v2.control.schema import ControlActionType
from app.capture_v2.gate.models import GateCheck, GateVerdict
from app.capture_v2.gate.r4_cli import build_parser
from app.capture_v2.gate.r4_real import _ordered_basic_sequence, _parse_device_source_ts, _verdict


def test_r4_real_parses_device_source_timestamp_with_offset():
    ts = _parse_device_source_ts("2026-08-22 09:01:02.123456", "+0800")
    assert ts.isoformat() == "2026-08-22T09:01:02.123456+08:00"


def test_r4_real_requires_ordered_physical_sequence():
    assert _ordered_basic_sequence([
        {"event": "OFFHOOK"}, {"event": "DTMF"}, {"event": "ONHOOK"},
    ]) is True
    assert _ordered_basic_sequence([
        {"event": "DTMF"}, {"event": "OFFHOOK"}, {"event": "ONHOOK"},
    ]) is False


def test_r4_real_no_physical_action_is_inconclusive_not_fail():
    checks = (GateCheck("physical", False, True, False),)
    assert _verdict(checks, physical_sequence_complete=False) == GateVerdict.INCONCLUSIVE


def test_r4_real_complete_physical_sequence_exposes_real_semantic_failure():
    checks = (GateCheck("semantic", False, True, False),)
    assert _verdict(checks, physical_sequence_complete=True) == GateVerdict.FAIL


def test_r4_real_control_action_and_cli_are_explicit():
    assert ControlActionType.GATE_READINESS_FXS.value == "GATE_READINESS_FXS"
    args = build_parser().parse_args([
        "--device-id", "D", "--model", "APF1250", "--host", "127.0.0.1",
        "--reproduction-session-id", "AUTO_NEW", "--worker-id", "W",
    ])
    assert args.duration == 90.0
    assert args.transport == "scp"

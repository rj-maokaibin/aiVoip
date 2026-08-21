from app.capture_v2.gate.cli import build_parser


def test_gate_cli_exposes_required_real_gate_commands():
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert {"ownership", "ownership-adopt", "segment", "collect", "evaluate", "lease-race", "fault"} <= set(commands)


def test_segment_fault_plan_is_explicit_opt_in():
    parser = build_parser()
    args = parser.parse_args([
        "segment", "--device-id", "D", "--model", "APF1250", "--host", "127.0.0.1",
        "--reproduction-session-id", "R", "--worker-id", "W",
    ])
    assert args.fault_plan == ""
    assert args.profile_id == "voip-standard"

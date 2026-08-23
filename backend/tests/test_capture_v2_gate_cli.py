import asyncio
from types import SimpleNamespace

from app.capture_v2.gate import cli
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


def test_segment_auto_new_is_resolved_before_gate_runner(monkeypatch):
    parser = build_parser()
    args = parser.parse_args([
        "segment", "--device-id", "D", "--model", "APF3260-M", "--host", "127.0.0.1",
        "--reproduction-session-id", "AUTO_NEW", "--worker-id", "W",
    ])
    calls = {}

    class Adapter:
        async def connect(self):
            calls["connected"] = True

        async def disconnect(self):
            calls["disconnected"] = True

    class Runner:
        async def segment_normal(self, **kwargs):
            calls["reproduction_session_id"] = kwargs["reproduction_session_id"]
            return SimpleNamespace(verdict=SimpleNamespace(value="PASS"), as_dict=lambda: {"verdict": "PASS"})

    monkeypatch.setattr(cli, "_resolve_reproduction_session_id", lambda value, *, device_id, before_state=None: "CLEAN-R3-SESSION")
    monkeypatch.setattr(cli, "build_asyncssh_adapter", lambda spec, password_env: Adapter())
    monkeypatch.setattr(cli, "_runner", lambda adapter, parsed_args: Runner())
    monkeypatch.setattr(cli, "_json", lambda payload: None)

    rc = asyncio.run(cli._cmd_segment(args))
    assert rc == 0
    assert calls["connected"] is True
    assert calls["disconnected"] is True
    assert calls["reproduction_session_id"] == "CLEAN-R3-SESSION"

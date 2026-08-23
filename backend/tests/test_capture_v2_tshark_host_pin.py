import os
from pathlib import Path

from app.capture_v2 import gate_cli


def test_pin_host_tshark_candidate_uses_executable_fixed_path(monkeypatch, tmp_path):
    tshark = tmp_path / "tshark"
    tshark.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tshark.chmod(0o755)

    monkeypatch.delenv("TSHARK_BINARY", raising=False)
    monkeypatch.setattr(gate_cli, "_TSHARK_HOST_CANDIDATES", (tshark,))

    assert gate_cli._pin_host_tshark_candidate() is True
    assert os.environ["TSHARK_BINARY"] == str(tshark)


def test_pin_host_tshark_candidate_preserves_explicit_override(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit-tshark"
    monkeypatch.setenv("TSHARK_BINARY", str(explicit))
    monkeypatch.setattr(gate_cli, "_TSHARK_HOST_CANDIDATES", (Path("/usr/bin/tshark"),))

    assert gate_cli._pin_host_tshark_candidate() is False
    assert os.environ["TSHARK_BINARY"] == str(explicit)

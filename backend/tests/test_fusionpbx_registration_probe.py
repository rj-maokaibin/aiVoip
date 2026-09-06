from __future__ import annotations

import asyncio
import inspect

import pytest

from app.automation.adapters.pbx.registration import (
    FusionPbxRegistrationProbe,
    FusionPbxRegistrationProbeError,
)


def test_registration_probe_uses_only_source_bound_read_commands() -> None:
    assert FusionPbxRegistrationProbe.COMMANDS == (
        ("show_registrations", "show registrations"),
        ("sofia_internal_reg", "sofia status profile internal reg"),
    )
    source = inspect.getsource(FusionPbxRegistrationProbe)
    for forbidden in ("reloadxml", "database->save", "database->delete", "devConfig.set"):
        assert forbidden not in source


def test_registration_probe_matches_exact_identity_and_never_persists_raw_output() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], _timeout: float):
        calls.append(argv)
        if argv[-1] == "show registrations":
            return 0, "reg/7900@example.test;transport=udp"
        return 0, "7102@example.test"

    probe = FusionPbxRegistrationProbe(runner=runner, poll_interval_seconds=0.001)
    evidence = asyncio.run(probe.wait_registered(number="7900", timeout_seconds=0.05))

    assert evidence.registered is True
    assert evidence.number == "7900"
    assert evidence.details["identity_observed"] is True
    assert evidence.details["secret_values_emitted"] is False
    assert "reg/7900@example.test" not in repr(evidence.details)
    assert [call[-1] for call in calls] == ["show registrations", "sofia status profile internal reg"]


def test_registration_probe_rejects_substring_and_non_numeric_target() -> None:
    def runner(_argv: tuple[str, ...], _timeout: float):
        return 0, "17900@example.test 7900.foo@example.test"

    probe = FusionPbxRegistrationProbe(runner=runner, poll_interval_seconds=0.001)
    evidence = asyncio.run(probe.wait_registered(number="7900", timeout_seconds=0.003))
    assert evidence.registered is False

    with pytest.raises(FusionPbxRegistrationProbeError, match="PBX_NUMERIC_REGISTRATION_TARGET_REQUIRED"):
        asyncio.run(probe.wait_registered(number="7900.x", timeout_seconds=0.01))

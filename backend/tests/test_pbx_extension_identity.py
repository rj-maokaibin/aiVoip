from __future__ import annotations

import asyncio

import pytest

from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    is_automation_identity,
    normalize_automation_identity,
)
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe


@pytest.mark.parametrize(
    "identity",
    ["7900", "7900.a", "SALES1", "+7900", "7900+lab", "A.B+C1"],
)
def test_managed_extension_identity_supports_alnum_dot_plus(identity: str) -> None:
    assert normalize_automation_identity(identity) == identity
    assert is_automation_identity(identity) is True


@pytest.mark.parametrize(
    "identity",
    ["", ".", "+", "7900_x", "7900-x", "7900@pbx", "7900/x", "7900 x", " 7900"],
)
def test_managed_extension_identity_rejects_out_of_contract_chars(identity: str) -> None:
    with pytest.raises(PbxExtensionIdentityError, match="PBX_AUTOMATION_IDENTITY_INVALID"):
        normalize_automation_identity(identity)


def test_registration_probe_matches_dot_and_plus_identity_exactly() -> None:
    def runner(argv: tuple[str, ...], _timeout: float):
        output = "7900.a@example.test +7901@example.test 17900.a@example.test"
        return 0, output

    probe = FusionPbxRegistrationProbe(runner=runner, poll_interval_seconds=0.001)
    dot = asyncio.run(probe.wait_registered(number="7900.a", timeout_seconds=0.01))
    plus = asyncio.run(probe.wait_registered(number="+7901", timeout_seconds=0.01))
    assert dot.registered is True
    assert plus.registered is True

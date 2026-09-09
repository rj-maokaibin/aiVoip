from __future__ import annotations

import asyncio

import pytest

from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    is_automation_identity,
    normalize_automation_identity,
    normalize_dial_alias,
    is_dial_alias,
)
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.adapters.pbx.profile import FusionPbxLabProfile


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


@pytest.mark.parametrize("alias", ["0", "7900", "00123", "12345678901234567890123456789012"])
def test_dial_alias_is_numeric_only(alias: str) -> None:
    assert normalize_dial_alias(alias) == alias
    assert is_dial_alias(alias) is True


@pytest.mark.parametrize("alias", ["", "7900.a", "+7900", "79*00", "79#00", " 7900", "7900 "])
def test_dial_alias_rejects_non_dtmf_numeric_contract(alias: str) -> None:
    with pytest.raises(PbxExtensionIdentityError, match="PBX_DIAL_ALIAS_INVALID"):
        normalize_dial_alias(alias)


def test_freeswitch_alias_resolution_returns_only_safe_root_identity_fields() -> None:
    profile = FusionPbxLabProfile.from_dict({
        "schema_version": "pbx-lab-profile-v1", "node_key": "x", "host": "127.0.0.1",
        "fusionpbx_root": "/tmp/fusionpbx", "php_bin": "/usr/bin/php", "fs_cli_bin": "/usr/bin/fs_cli",
        "internal_profile": "internal", "internal_port": 5060,
        "extension_pool": {"start": 7900, "end": 7999}, "protected_extensions": ["7102"],
        "source_fence": {"version": "v", "files": {"x": "0" * 64}},
    })
    raw = ('<user id="7900.a" number-alias="7900" domain-name="pbx.test">'
           '<params><param name="password" value="must-not-escape"/></params></user>')
    probe = FreeSwitchRuntimeReadProbe(profile, runner=lambda _argv, _timeout: (0, raw))
    result = probe.resolve_directory_identity("7900", domain_name="pbx.test")
    assert result == {
        "resolved_identity": "7900.a", "number_alias": "7900",
        "domain_name": "pbx.test", "secret_values_emitted": False,
    }
    assert "must-not-escape" not in repr(result)

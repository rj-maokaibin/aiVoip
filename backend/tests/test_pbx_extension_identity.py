from __future__ import annotations

import asyncio
import string

import pytest

from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    RFC3261_DIRECT_USER_CHARS,
    encode_sip_user_identity,
    is_automation_identity,
    normalize_automation_identity,
    normalize_dial_alias,
    normalize_sip_user_wire_identity,
    normalize_testlab_provider_mutation_identity,
)
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe


@pytest.mark.parametrize(
    "identity",
    ["7900", "7900.a", "SALES1", "+7900", "sales-01", "sales_01", "A.B-C_D+01"],
)
def test_product_profile_supports_common_managed_identity(identity: str) -> None:
    assert normalize_automation_identity(identity) == identity
    assert is_automation_identity(identity) is True


def test_rfc3261_direct_user_character_matrix_is_core_supported() -> None:
    expected_special = "-_.!~*'()&=+$,;?/"
    for char in string.ascii_letters + string.digits + expected_special:
        identity = f"a{char}1"
        assert normalize_sip_user_wire_identity(identity) == identity
        assert char in RFC3261_DIRECT_USER_CHARS


@pytest.mark.parametrize(
    ("logical", "wire"),
    [
        ("j@s0n", "j%40s0n"),
        ("a:b", "a%3Ab"),
        ("room 1", "room%201"),
        ("100%real", "100%25real"),
        ("福州", "%E7%A6%8F%E5%B7%9E"),
    ],
)
def test_sip_user_codec_supports_escaped_logical_identity(logical: str, wire: str) -> None:
    encoded = encode_sip_user_identity(logical)
    assert encoded.wire == wire
    assert encoded.escaped is True
    assert normalize_sip_user_wire_identity(wire) == wire


@pytest.mark.parametrize("wire", ["a%b", "a%G1b", "a b", "a@b", "a:b", "a\\b"])
def test_raw_wire_rejects_chars_that_require_escaping_or_are_invalid(wire: str) -> None:
    with pytest.raises(PbxExtensionIdentityError):
        normalize_sip_user_wire_identity(wire)


@pytest.mark.parametrize("logical", ["a\x00b", "a\rb", "a\nb", "a\x7fb"])
def test_safe_positive_codec_rejects_control_characters(logical: str) -> None:
    with pytest.raises(PbxExtensionIdentityError, match="CONTROL_CHAR"):
        encode_sip_user_identity(logical)


@pytest.mark.parametrize("identity", ["a!1", "a~1", "a*1", "a'1", "a(1)", "a&1", "a=1", "a,1", "a;1", "a?1"])
def test_fusionpbx_testlab_mutation_accepts_safe_direct_protocol_chars(identity: str) -> None:
    assert normalize_testlab_provider_mutation_identity(identity) == identity


def test_protocol_legal_slash_is_provider_rejected_due_file_cache_path_risk() -> None:
    assert normalize_sip_user_wire_identity("a/1") == "a/1"
    with pytest.raises(PbxExtensionIdentityError, match="PATH_RISK"):
        normalize_testlab_provider_mutation_identity("a/1")


def test_protocol_legal_dollar_is_provider_rejected_due_xml_identity_loss() -> None:
    assert normalize_sip_user_wire_identity("a$1") == "a$1"
    with pytest.raises(PbxExtensionIdentityError, match="XML_SANITIZE_LOSS"):
        normalize_testlab_provider_mutation_identity("a$1")


def test_dial_alias_remains_numeric_only() -> None:
    assert normalize_dial_alias("7900") == "7900"
    for bad in ("79.00", "+7900", "7900a", "*7900", "7900#"):
        with pytest.raises(PbxExtensionIdentityError, match="PBX_DIAL_ALIAS_INVALID"):
            normalize_dial_alias(bad)


def test_registration_probe_matches_rfc_punctuation_identity_exactly() -> None:
    def runner(argv: tuple[str, ...], _timeout: float):
        del argv
        return 0, "a;1@example.test a?1@example.test xa;1@example.test"

    probe = FusionPbxRegistrationProbe(runner=runner, poll_interval_seconds=0.001)
    semi = asyncio.run(probe.wait_registered(number="a;1", timeout_seconds=0.01))
    question = asyncio.run(probe.wait_registered(number="a?1", timeout_seconds=0.01))
    assert semi.registered is True
    assert question.registered is True

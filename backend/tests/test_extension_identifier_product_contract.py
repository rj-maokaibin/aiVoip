from __future__ import annotations

from pathlib import Path

from app.automation.product_contracts.extension_identifier import (
    LEGACY_DIGITS_ONLY_CONTRACT,
    effective_contract,
    load_extension_identifier_contract,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "profiles/product_contracts/apf3260m_extension_identifier_v1.yaml"


def test_v13_extension_contract_acceptance_matrix() -> None:
    contract, raw = load_extension_identifier_contract(CONTRACT_PATH)
    for value in raw["acceptance"]["positive"]:
        result = contract.validate(str(value))
        assert result.accepted, (value, result.reason)
    for item in raw["acceptance"]["negative"]:
        result = contract.validate(str(item["value"]))
        assert not result.accepted, item
        assert result.reason == item["reason"]


def test_v13_special_character_boundaries_and_case_contract() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    assert contract.allowed_specials == ".+"
    assert contract.dot_allow_leading is True
    assert contract.dot_allow_trailing is True
    assert contract.dot_allow_consecutive is True
    assert contract.plus_anywhere is True
    assert contract.case_sensitive is True
    assert contract.min_length == 6
    assert contract.max_length == 13
    assert contract.pure_alpha_allowed is True
    assert contract.pure_special_char_allowed is True
    assert contract.whitespace_allowed is False


def test_v13_identity_fields_share_rule_without_forcing_number_authid_equality() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    assert contract.disname_same_rule is True
    assert contract.auth_id_same_rule is True
    assert contract.number_auth_id_must_equal is False


def test_missing_extension_capability_falls_back_to_digits_only() -> None:
    current, _ = load_extension_identifier_contract(CONTRACT_PATH)
    legacy = effective_contract(current, capability_present=False)
    assert legacy is LEGACY_DIGITS_ONLY_CONTRACT
    assert legacy.validate("7102").accepted is True
    assert legacy.validate("7102.a").accepted is False
    assert legacy.validate("ABCDEF").accepted is False


def test_current_capability_keeps_non_numeric_contract() -> None:
    current, _ = load_extension_identifier_contract(CONTRACT_PATH)
    resolved = effective_contract(current, capability_present=True)
    assert resolved.validate("7900.a").accepted is True
    assert resolved.validate(".7900a").accepted is True
    assert resolved.validate("7900a.").accepted is True
    assert resolved.validate("79..0a").accepted is True

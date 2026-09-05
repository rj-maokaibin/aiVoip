from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from app.automation.adapters.pbx.base import (
    PbxContractError,
    PbxIdentityRequest,
    PbxResourceKind,
    validate_pbx_identity_request,
)
from app.automation.contracts import parse_test_case
from app.automation.product_contracts.extension_identifier import load_extension_identifier_contract


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "profiles/product_contracts/apf3260m_extension_identifier_v1.yaml"


def test_pbx_contract_exposes_secret_reference_not_plaintext_password() -> None:
    names = {item.name for item in fields(PbxIdentityRequest)}
    assert "credential_ref" in names
    assert "password" not in names
    assert "passwd" not in names


def test_nonnum_alias_request_is_validated_by_frozen_extension_contract() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    request = PbxIdentityRequest(
        kind=PbxResourceKind.ALIAS,
        extension="79+00.a",
        auth_id="Auth+01",
        display_name=".79A0a",
        alias_target="existing-registration-identity",
    )
    validate_pbx_identity_request(contract, request)


def test_pbx_alias_and_extension_secret_rules_are_explicit() -> None:
    with pytest.raises(PbxContractError, match="PBX_ALIAS_TARGET_REQUIRED"):
        PbxIdentityRequest(
            kind=PbxResourceKind.ALIAS,
            extension="79+00.a",
            auth_id="Auth+01",
            display_name=".79A0a",
        )
    with pytest.raises(PbxContractError, match="PBX_EXTENSION_CREDENTIAL_REF_REQUIRED"):
        PbxIdentityRequest(
            kind=PbxResourceKind.EXTENSION,
            extension="79+00.a",
            auth_id="Auth+01",
            display_name=".79A0a",
        )


def test_nonnum_case_is_reserved_until_real_pbx_provider_binding() -> None:
    raw = yaml.safe_load((ROOT / "profiles/tests/golden_web_nonnum_001.yaml").read_text(encoding="utf-8"))
    case = parse_test_case(raw)
    assert case.case_id == "Golden-WEB-NONNUM-001"
    assert case.entry.value == "web"
    assert case.contract_status.value == "RESERVED"
    assert case.executable is False

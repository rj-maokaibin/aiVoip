from __future__ import annotations

from pathlib import Path

import pytest

from app.automation.gates.golden_web_nonnum_contract import (
    NONNUM_CLEANUP_ORDER,
    build_nonnum_probe,
    expected_nonnum_cleanup_order,
)
from app.automation.orchestrator import RuntimeBlocked
from app.automation.product_contracts.extension_identifier import load_extension_identifier_contract


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "profiles/product_contracts/apf3260m_extension_identifier_v1.yaml"


def _snapshot() -> dict:
    return {
        "voice_vlan": {"mode": "current"},
        "voipServInfo": {"server": "current"},
        "voipUserInfo": {
            "data": [{
                "disName": "7102",
                "number": "7102",
                "authId": "existing-auth",
                "passwd": "must-remain-unchanged",
            }]
        },
        "voipFxsTbl": {"fxs": "current"},
        "voipAdvanced": {"advanced": "current"},
    }


def test_nonnum_probe_changes_only_number_and_display_name() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    snapshot = _snapshot()
    probe = build_nonnum_probe(
        snapshot,
        "79+00.a",
        contract=contract,
        capability_present=True,
    )
    account = probe["voipUserInfo"]["data"][0]
    assert account["number"] == "79+00.a"
    assert account["disName"] == "79+00.a"
    assert account["authId"] == "existing-auth"
    assert account["passwd"] == "must-remain-unchanged"
    assert snapshot["voipUserInfo"]["data"][0]["number"] == "7102"
    for module in ("voice_vlan", "voipServInfo", "voipFxsTbl", "voipAdvanced"):
        assert probe[module] == snapshot[module]


def test_nonnum_probe_is_blocked_before_mutation_on_legacy_dut() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    with pytest.raises(RuntimeBlocked, match="NONNUM_EXTENSION_CAPABILITY_REQUIRED"):
        build_nonnum_probe(
            _snapshot(),
            "79+00.a",
            contract=contract,
            capability_present=False,
        )


def test_nonnum_golden_rejects_digits_only_probe() -> None:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    with pytest.raises(RuntimeBlocked, match="WEB_NONNUM_TARGET_REQUIRED"):
        build_nonnum_probe(
            _snapshot(),
            "790001",
            contract=contract,
            capability_present=True,
        )


def test_nonnum_cleanup_order_restores_dut_then_deletes_pbx_then_releases_authority() -> None:
    assert expected_nonnum_cleanup_order() == NONNUM_CLEANUP_ORDER
    assert NONNUM_CLEANUP_ORDER == (
        "restore_web_voip_bundle",
        "delete_pbx_identity",
        "release_device_authority",
    )

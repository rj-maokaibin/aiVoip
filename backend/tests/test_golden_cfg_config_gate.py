from pathlib import Path
import inspect

from app.automation.gates.golden_cfg_config import (
    G0_ROUTE,
    GOLDEN_CFG_CONFIG_CASE_ID,
    build_display_name_probe,
    safe_readback,
)
from app.automation.registry import TestRegistry
from app.infrastructure.action_route import ActionBackend, ActionEntry, ActionPurpose, ActionTransport
from app.infrastructure.config_framework.schema import ConfigResult


def test_g0_route_is_backend_config_framework_not_product_entry():
    assert G0_ROUTE.entry is ActionEntry.NONE
    assert G0_ROUTE.transport is ActionTransport.SSH
    assert G0_ROUTE.backend is ActionBackend.CONFIG_FRAMEWORK
    assert G0_ROUTE.purpose is ActionPurpose.TEST
    assert G0_ROUTE.target == "voipUserInfo"


def test_g0_probe_changes_only_display_name_and_preserves_identity_and_secret_values():
    snapshot = {
        "data": [{
            "hdl": 0,
            "active": 1,
            "timeout": 3600,
            "disName": "2002",
            "number": "2002",
            "authId": "2002",
            "passwd": "sensitive",
            "encType": 1,
        }]
    }
    probe, marker = build_display_name_probe(snapshot)
    assert marker == "G0"
    assert probe["data"][0]["disName"] == "G0"
    for key in ("hdl", "active", "timeout", "number", "authId", "passwd", "encType"):
        assert probe["data"][0][key] == snapshot["data"][0][key]
    assert snapshot["data"][0]["disName"] == "2002"


def test_g0_probe_remains_a_real_change_if_original_display_name_is_g0():
    probe, marker = build_display_name_probe({"data": [{"disName": "G0", "number": "2002"}]})
    assert marker == "G1"
    assert probe["data"][0]["disName"] == "G1"


def test_g0_readback_masks_password_before_assertion_evidence():
    result = ConfigResult(
        rcode="00000000",
        rmsg="success",
        data=[{"number": "2002", "passwd": "top-secret", "disName": "G0"}],
        raw={
            "rcode": "00000000",
            "rmsg": "success",
            "data": [{"number": "2002", "passwd": "top-secret", "disName": "G0"}],
        },
    )
    safe = safe_readback(result)
    assert safe["data"][0]["passwd"] == "***"
    assert safe["data"][0]["disName"] == "G0"


def test_g0_case_is_strict_registry_case_and_assertion_engine_source_is_config_framework():
    root = Path(__file__).resolve().parents[2] / "profiles" / "tests"
    definition = TestRegistry(root).definition(GOLDEN_CFG_CONFIG_CASE_ID)
    case = definition.case
    assert case.entry is ActionEntry.NONE
    assert case.steps[0].purpose is ActionPurpose.TEST
    assert case.assertions[0].source == "config_framework"
    assert case.assertions[0].path == "data[0].disName"
    assert case.cleanup.strategy == "restore_snapshot"
    assert case.cleanup.verify is True


def test_g0_core_has_no_second_ssh_client_or_rule_engine_verdict():
    from app.automation.gates.golden_cfg_config import GoldenCfgConfigGate

    source = inspect.getsource(GoldenCfgConfigGate).lower()
    assert "asyncssh" not in source
    assert "ruleengine" not in source
    assert "assertionengine" in source
    assert "captureleasecompatibilityadapter" in source

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from app.automation.adapters.entries.web import EntryResult
from app.automation.contracts import parse_test_case
from app.automation.gates.golden_web_config import (
    GOLDEN_WEB_CONFIG_CASE_ID,
    WEB_CONFIG_ROUTE,
    WEB_WRITABLE_MODULES,
    GoldenWebConfigGate,
    build_numeric_probe,
    config_payload_from_web_module,
    snapshot_writable_bundle,
)
from app.automation.orchestrator import RuntimeBlocked
from app.infrastructure.action_route import ActionBackend, ActionEntry, ActionPurpose, ActionRoute, ActionTransport


ROOT = Path(__file__).resolve().parents[2]


def _snapshot() -> dict:
    return {
        "voice_vlan": {"mode": "current"},
        "voipServInfo": {"server": "current"},
        "voipUserInfo": {
            "data": [{
                "hdl": "0",
                "active": "1",
                "timeout": "3600",
                "disName": "7102",
                "number": "7102",
                "authId": "auth-separate",
                "passwd": "secret-not-to-be-rewritten",
            }]
        },
        "voipFxsTbl": {"fxs": "current"},
        "voipAdvanced": {"advanced": "current"},
    }


def test_pr_d_route_is_real_web_http_config_framework_test_path() -> None:
    assert WEB_CONFIG_ROUTE.entry is ActionEntry.WEB
    assert WEB_CONFIG_ROUTE.transport is ActionTransport.HTTP_API
    assert WEB_CONFIG_ROUTE.backend is ActionBackend.CONFIG_FRAMEWORK
    assert WEB_CONFIG_ROUTE.purpose is ActionPurpose.TEST


def test_numeric_probe_mutates_only_target_identity_fields_inside_full_five_module_snapshot() -> None:
    snapshot = _snapshot()
    probe = build_numeric_probe(snapshot, "7900")
    assert tuple(probe) == WEB_WRITABLE_MODULES
    assert probe["voice_vlan"] == snapshot["voice_vlan"]
    assert probe["voipServInfo"] == snapshot["voipServInfo"]
    assert probe["voipFxsTbl"] == snapshot["voipFxsTbl"]
    assert probe["voipAdvanced"] == snapshot["voipAdvanced"]
    account = probe["voipUserInfo"]["data"][0]
    assert account["number"] == "7900"
    assert account["disName"] == "7900"
    assert account["authId"] == "auth-separate"
    assert account["passwd"] == "secret-not-to-be-rewritten"
    assert snapshot["voipUserInfo"]["data"][0]["number"] == "7102"


def test_web_restore_snapshot_uses_runtime_raw_bundle_while_public_output_stays_masked() -> None:
    raw = _snapshot()
    masked = _snapshot()
    masked["voipUserInfo"]["data"][0]["passwd"] = "***"
    result = EntryResult(
        accepted=True,
        output={"modules": masked},
        runtime_output={"modules": raw},
    )

    snapshot = snapshot_writable_bundle(result)

    assert result.output["modules"]["voipUserInfo"]["data"][0]["passwd"] == "***"
    assert snapshot["voipUserInfo"]["data"][0]["passwd"] == "secret-not-to-be-rewritten"
    assert "secret-not-to-be-rewritten" not in repr(result)


def test_numeric_probe_rejects_non_ascii_digit_characters() -> None:
    with pytest.raises(RuntimeBlocked, match="WEB_NUMERIC_TARGET_REQUIRED"):
        build_numeric_probe(_snapshot(), "٧٩٠٠")


def test_web_snapshot_maps_to_read_only_config_framework_crosscheck_payload() -> None:
    payload = config_payload_from_web_module(_snapshot()["voipUserInfo"])
    assert payload["data"][0]["number"] == "7102"
    assert payload["data"][0]["authId"] == "auth-separate"


def test_golden_web_case_requires_config_and_protocol_assertions_and_cleanup() -> None:
    raw = yaml.safe_load((ROOT / "profiles/tests/golden_web_config_001.yaml").read_text(encoding="utf-8"))
    case = parse_test_case(raw)
    assert case.case_id == GOLDEN_WEB_CONFIG_CASE_ID
    assert case.entry is ActionEntry.WEB
    assert case.snapshot == ("web_voip_writable_bundle",)
    assert [a.source for a in case.assertions] == ["entry", "entry", "sip", "sip"]
    assert case.cleanup.strategy == "restore_snapshot"
    assert case.cleanup.verify is True


def test_pr_d_ssh_is_crosscheck_only_not_web_mutation_fallback() -> None:
    source = (ROOT / "backend/app/automation/gates/golden_web_config.py").read_text(encoding="utf-8")
    assert 'self.config.get("voipUserInfo")' in source
    assert "self.config.set(" not in source
    assert 'entry=ActionEntry.WEB' in source
    assert 'transport=ActionTransport.HTTP_API' in source


def test_pr_d_renews_one_authority_term_and_validates_each_web_mutation() -> None:
    source = inspect.getsource(GoldenWebConfigGate)
    configure_source = inspect.getsource(GoldenWebConfigGate._configure)
    restore_source = inspect.getsource(GoldenWebConfigGate._restore_action)
    validator_source = inspect.getsource(GoldenWebConfigGate._validate_mutation_authority)

    assert "AuthorityKeepalive" in source
    assert "self.keepalive.start(token)" in source
    assert "self.authority.validate(token)" in validator_source
    assert "self._validate_mutation_authority()" in configure_source
    assert "self._validate_mutation_authority()" in restore_source
    assert "keepalive.stop" not in restore_source
    assert "self.authority.acquire(" not in validator_source


def test_pr_d_stops_keepalive_before_release_and_never_reacquires_in_cleanup() -> None:
    release_source = inspect.getsource(GoldenWebConfigGate._release_action)
    assert release_source.index("await self.keepalive.stop()") < release_source.index("self.authority.release(token)")
    assert "self.authority.acquire(" not in release_source

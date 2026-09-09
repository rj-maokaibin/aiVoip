from __future__ import annotations

import copy
import inspect
from pathlib import Path

from app.automation.gates.golden_pbx_register import (
    PbxRegistrationGoldenGate,
    build_managed_identity_probe,
    build_managed_restore_bundle,
    managed_fields_restored,
    managed_identity_matches,
)


def _snapshot() -> dict:
    return {
        "voice_vlan": {"enable": "0", "vlanid": "1", "aging": "300", "cos": "0"},
        "voipServInfo": {"data": [{"hdl": "0", "svrName": "pbx.test", "svrPort": "5060", "svrNameBak": "", "svrPortBak": "5060"}]},
        "voipUserInfo": {"data": [{
            "hdl": "0", "active": "1", "timeout": "3600",
            "disName": "7102", "number": "7102", "authId": "7102", "passwd": "baseline-secret",
        }]},
        "voipFxsTbl": {"data": [{"hdl": "0", "inVol": "0", "outVol": "0", "dailTimeout": "5"}]},
        "voipAdvanced": {"sipTP": "0", "dtmfMode": "0", "dtmfPayload": "101", "cidStd": "0", "dspGain": {"inGain": "0", "outGain": "0"}, "version": "1"},
    }


def test_managed_probe_changes_only_declared_user_identity_fields() -> None:
    snapshot = _snapshot()
    probe = build_managed_identity_probe(snapshot, identity="7900.a", password="temp-secret")
    assert probe["voice_vlan"] == snapshot["voice_vlan"]
    row = probe["voipUserInfo"]["data"][0]
    assert (row["number"], row["disName"], row["authId"], row["passwd"]) == (
        "7900.a", "7900.a", "7900.a", "temp-secret"
    )
    assert managed_identity_matches(probe, identity="7900.a", password="temp-secret") is True


def test_restore_uses_fresh_bundle_and_restores_all_four_managed_fields() -> None:
    snapshot = _snapshot()
    current = build_managed_identity_probe(snapshot, identity="+7900", password="temp-secret")
    current = copy.deepcopy(current)
    current["voice_vlan"]["aging"] = "999"
    restore = build_managed_restore_bundle(current, snapshot)
    assert restore["voice_vlan"]["aging"] == "999"
    row = restore["voipUserInfo"]["data"][0]
    original = snapshot["voipUserInfo"]["data"][0]
    for field in ("number", "disName", "authId", "passwd"):
        assert row[field] == original[field]
    assert managed_fields_restored(restore, snapshot) is True


def test_registration_gate_requires_esl_event_and_does_not_change_numeric_golden() -> None:
    source = inspect.getsource(PbxRegistrationGoldenGate)
    assert "require_esl_event=True" in source
    assert "baseline_registration_identity" in source
    assert "build_managed_restore_bundle" in source
    assert "PBX_REGISTER_CASE_ID_MISMATCH" in source


def test_controlled_runner_binds_pbx_alias_and_release_last_without_persisting_secret() -> None:
    source = Path("tools/run_golden_pbx_register_002.py").read_text(encoding="utf-8")
    assert 'dial_alias=args.dial_alias' in source
    assert 'runtime.resolve_directory_identity' in source
    assert 'await base._run(args)' in source
    assert source.index('await base._run(args)') < source.index('manager.deprovision_extension(pbx_token)')
    assert source.index('manager.deprovision_extension(pbx_token)') < source.index('manager.release_extension(pbx_token)')
    assert 'AuthorityKeepalive' in source
    assert 'secret_values_emitted' in source
    assert 'payload["target_password"]' not in source
    assert 'payload["password"]' not in source
    summary_source = source.split('def _summary', 1)[1].split('def _pbx_context', 1)[0]
    assert 'target_password' not in summary_source

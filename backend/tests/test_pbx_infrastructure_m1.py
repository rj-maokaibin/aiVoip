from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from app.automation.adapters.pbx.fusionpbx_config import (
    FusionPbxConfigProvider,
    FusionPbxConfigProviderError,
)
from app.automation.adapters.pbx.health import PbxHealthGate
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence


def _profile(tmp_path: Path) -> FusionPbxLabProfile:
    root = tmp_path / "fusionpbx"
    files = {
        "resources/require.php": "<?php // require ?>",
        "resources/classes/database.php": "<?php // db ?>",
        "app/extensions/resources/classes/extension.php": (
            "public function exists($x) {} public function delete($x) {} "
            "number_alias = :extension"
        ),
        "app/extensions/extension_edit.php": "<?php // edit ?>",
        "app/extensions/extension_copy.php": "<?php // copy ?>",
    }
    hashes = {}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        hashes[rel] = hashlib.sha256(text.encode()).hexdigest()
    return FusionPbxLabProfile.from_dict(
        {
            "schema_version": "pbx-lab-profile-v1",
            "node_key": "test-pbx",
            "host": "127.0.0.1",
            "fusionpbx_root": str(root),
            "php_bin": "/usr/bin/php",
            "fs_cli_bin": "/usr/bin/fs_cli",
            "internal_profile": "internal",
            "internal_port": 5060,
            "extension_pool": {"start": 7900, "end": 7999},
            "protected_extensions": ["7102"],
            "source_fence": {"version": "test-v1", "files": hashes},
        }
    )


def test_source_fence_is_exact_and_fail_closed(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    result = FusionPbxSourceFence(profile).verify()
    assert result.ok is True
    assert result.contract_facts == {
        "extension_exists_method": True,
        "extension_delete_method": True,
        "exists_checks_number_alias": True,
    }
    (Path(profile.fusionpbx_root) / "resources/require.php").write_text(
        "changed", encoding="utf-8"
    )
    failed = FusionPbxSourceFence(profile).inspect()
    assert failed.ok is False
    assert "sha256:resources/require.php" in failed.mismatches


def test_config_provider_is_read_only_and_redacts_password(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    calls = []

    def runner(script: str, req: dict, _timeout: float):
        calls.append((script, req))
        if req["action"] == "domains":
            rows = [{"domain_uuid": "d1", "domain_name": "pbx.test"}]
        elif req["action"] == "extension":
            rows = [
                {
                    "domain_uuid": "d1",
                    "extension": "7102",
                    "number_alias": None,
                    "enabled": True,
                    "description": "baseline",
                    "accountcode": "7102",
                    "user_context": "default",
                    "password_present": True,
                }
            ]
        else:
            rows = [
                {
                    "domain_uuid": "d1",
                    "extension": "7102",
                    "number_alias": "7002",
                }
            ]
        return 0, json.dumps(
            {
                "mutation_executed": False,
                "secret_values_emitted": False,
                "rows": rows,
            }
        )

    provider = FusionPbxConfigProvider(profile, runner=runner)
    assert provider.discover_domains()[0].domain_name == "pbx.test"
    view = provider.get_extension("7102")[0]
    assert view.password_present is True
    assert "password" not in view.__dict__
    assert provider.extension_exists("7102") is True
    assert provider.list_identities() == {"d1": {"7102", "7002"}}
    source = inspect.getsource(FusionPbxConfigProvider)
    for forbidden in (
        "INSERT INTO",
        "UPDATE v_extensions",
        "DELETE FROM v_extensions",
        "reloadxml",
    ):
        assert forbidden not in source
    with pytest.raises(
        FusionPbxConfigProviderError, match="PBX_EXTENSION_IDENTITY_INVALID"
    ):
        provider.extension_exists("7102;drop")


def test_health_gate_returns_safe_pbx_ready(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    def php_runner(_script: str, req: dict, _timeout: float):
        if req["action"] == "domains":
            rows = [{"domain_uuid": "d1", "domain_name": "pbx.test"}]
        elif req["action"] == "identities":
            rows = [
                {"domain_uuid": "d1", "extension": "7102", "number_alias": None},
                {"domain_uuid": "d1", "extension": "7901", "number_alias": None},
            ]
        else:
            rows = []
        return 0, json.dumps(
            {
                "mutation_executed": False,
                "secret_values_emitted": False,
                "rows": rows,
            }
        )

    def fs_runner(argv: tuple[str, ...], _timeout: float):
        command = argv[-1]
        if command == "status":
            return 0, "FreeSWITCH is ready"
        if command.startswith("sofia status profile"):
            return 0, "Name             \tinternal\nSIP-IP           \t127.0.0.1\nURL              \tsip:mod_sofia@127.0.0.1:5060"
        if command == "show registrations":
            return 0, "reg_user,realm\n7102,pbx.test\n\n1 total.\n"
        raise AssertionError(command)

    gate = PbxHealthGate(
        profile,
        config_provider=FusionPbxConfigProvider(profile, runner=php_runner),
        fs_runner=fs_runner,
        socket_probe=lambda _h, _p, _t: True,
    )
    result = gate.check()
    assert result.ready is True
    assert result.pool_occupied_count == 1
    assert result.pool_first_available == 7900
    assert result.protected_registration == {"7102": True}
    safe = result.safe_dict()
    assert safe["mutation_executed"] is False
    assert safe["secret_values_emitted"] is False
    assert "password" not in json.dumps(safe).lower()


def test_health_gate_fails_closed_on_source_or_runtime_failure(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    (Path(profile.fusionpbx_root) / "resources/require.php").write_text(
        "changed", encoding="utf-8"
    )

    def php_runner(_script: str, req: dict, _timeout: float):
        rows = (
            [{"domain_uuid": "d1", "domain_name": "pbx.test"}]
            if req["action"] == "domains"
            else []
        )
        return 0, json.dumps(
            {
                "mutation_executed": False,
                "secret_values_emitted": False,
                "rows": rows,
            }
        )

    result = PbxHealthGate(
        profile,
        config_provider=FusionPbxConfigProvider(profile, runner=php_runner),
        fs_runner=lambda _a, _t: (1, ""),
        socket_probe=lambda _h, _p, _t: False,
    ).check()
    assert result.ready is False
    assert result.checks["source_fence"] is False
    assert result.checks["freeswitch_runtime"] is False
    assert result.checks["internal_sip_port_reachable"] is False


def test_pbx_migration_and_models_cover_frozen_resource_tables() -> None:
    import app.automation.pbx_models as models

    names = set(models.Base.metadata.tables)
    required = {
        "pbx_nodes",
        "pbx_extension_resources",
        "pbx_resource_leases",
        "pbx_mutation_snapshots",
        "pbx_call_sessions",
    }
    assert required <= names
    migration = Path("backend/migrations/versions/0034_pbx_test_lab_v1.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "0033_automation_test_runtime_v1"' in migration
    for table in required:
        assert f'"{table}"' in migration

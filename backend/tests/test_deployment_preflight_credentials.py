from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "deployment_preflight.py"
spec = importlib.util.spec_from_file_location("deployment_preflight_under_test", MODULE_PATH)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preflight
spec.loader.exec_module(preflight)


def _by_key(checks):
    return {item.key: item for item in checks}


def test_api_placeholder_url_fails_closed():
    checks = _by_key(preflight.credential_provider_checks({
        "CREDENTIAL_PROVIDER": "api",
        "CREDENTIAL_API_URL": "https://credential-service.example.internal/v1/device-password",
    }))
    assert checks["CREDENTIAL_PROVIDER"].status == "PASS"
    assert checks["CREDENTIAL_API_URL"].status == "BLOCKED"
    assert checks["CREDENTIAL_API_URL"].blocks_deploy is True
    assert "placeholder host" in checks["CREDENTIAL_API_URL"].detail


def test_api_real_internal_url_passes_static_validation():
    checks = _by_key(preflight.credential_provider_checks({
        "CREDENTIAL_PROVIDER": "api",
        "CREDENTIAL_API_URL": "https://credential-api.prod.internal/v1/device-password",
    }))
    assert checks["CREDENTIAL_PROVIDER"].status == "PASS"
    assert checks["CREDENTIAL_API_URL"].status == "PASS"


def test_poseidon_is_allowed_when_bootstrap_secret_is_secure(monkeypatch):
    monkeypatch.setattr(preflight, "secure_file", lambda path: (True, "mode 0600, 123 bytes"))
    checks = _by_key(preflight.credential_provider_checks({
        "CREDENTIAL_PROVIDER": "poseidon",
        "CREDENTIAL_API_URL": "https://credential-service.example.internal/v1/device-password",
    }))
    assert checks["CREDENTIAL_PROVIDER"].status == "PASS"
    assert checks["POSEIDON_SECRET_FILE"].status == "PASS"
    assert "CREDENTIAL_API_URL" not in checks


def test_poseidon_fails_closed_when_bootstrap_secret_missing(monkeypatch):
    monkeypatch.setattr(preflight, "secure_file", lambda path: (False, "missing"))
    checks = _by_key(preflight.credential_provider_checks({"CREDENTIAL_PROVIDER": "poseidon"}))
    assert checks["CREDENTIAL_PROVIDER"].status == "PASS"
    assert checks["POSEIDON_SECRET_FILE"].status == "BLOCKED"
    assert checks["POSEIDON_SECRET_FILE"].blocks_deploy is True


def test_db_provider_is_not_production_capable():
    checks = _by_key(preflight.credential_provider_checks({"CREDENTIAL_PROVIDER": "db"}))
    assert checks["CREDENTIAL_PROVIDER"].status == "BLOCKED"
    assert checks["CREDENTIAL_PROVIDER"].blocks_release is True


def test_resolved_service_url_rejects_angle_placeholder_and_non_http():
    assert preflight.resolved_service_url("https://cred.internal/<device>")[0] is False
    assert preflight.resolved_service_url("ftp://cred.internal/device")[0] is False

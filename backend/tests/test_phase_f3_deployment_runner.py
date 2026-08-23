from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_secure_file_fails_closed_when_secret_path_is_not_accessible(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    from deploy.deployment_preflight import secure_file

    def denied(*_args, **_kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(Path, "stat", denied)
    assert secure_file(Path("/protected/secret")) == (False, "unreadable (PermissionError)")


def test_production_frontend_is_same_origin_and_sse_safe():
    api = (ROOT / "frontend/src/api.ts").read_text(encoding="utf-8")
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    assert "'/api/v1'" in api
    assert "localhost:8000" not in api
    assert "location /api/" in nginx
    assert "proxy_pass http://backend:8000" in nginx
    assert "proxy_buffering off" in nginx


def test_production_compose_mounts_required_secrets_and_release_runner():
    payload = yaml.safe_load((ROOT / "docker-compose.production.yml").read_text(encoding="utf-8"))
    assert "release-runner" in payload["services"]
    required = {
        "auth_gateway_hmac", "minio_access_key", "minio_secret_key",
        "credential_api_token", "feishu_app_secret", "feishu_verification_token",
    }
    assert required <= set(payload["secrets"])
    backend_secrets = set(payload["services"]["backend"]["secrets"])
    assert required <= backend_secrets


def test_production_feishu_rbac_is_declared_and_preflight_enforced():
    template = (ROOT / "deploy/production.env.example").read_text(encoding="utf-8")
    preflight = (ROOT / "deploy/deployment_preflight.py").read_text(encoding="utf-8")
    assert "FEISHU_IDENTITY_RBAC_ENABLED=true" in template
    assert '"FEISHU_IDENTITY_RBAC"' in preflight
    assert 'values.get("FEISHU_IDENTITY_RBAC_ENABLED", "false")' in preflight


def test_production_cli_is_fail_closed_and_non_destructive():
    path = ROOT / "deploy/voip-ai"
    text = path.read_text(encoding="utf-8")
    assert os.access(path, os.X_OK)
    for command in ["preflight", "prepare-host", "deploy", "verify", "release", "backup-db"]:
        assert command in text
    assert "down -v" not in text
    assert "docker volume rm" not in text
    assert "rm -rf /data" not in text
    assert "release_readiness_gate.py --strict" in text


def test_deployment_preflight_rejects_example_placeholders(tmp_path):
    src = ROOT / "deploy/production.env.example"
    env = tmp_path / "production.env"
    env.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    env.chmod(stat.S_IRUSR | stat.S_IWUSR)
    cp = subprocess.run(
        [sys.executable, str(ROOT / "deploy/deployment_preflight.py"), "--env-file", str(env), "--mode", "release"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert cp.returncode == 2
    payload = json.loads(cp.stdout)
    assert payload["release_status"] == "BLOCKED"
    assert "NO_PLACEHOLDERS" in payload["release_blocking_keys"]
    checks = {item["key"]: item for item in payload["checks"]}
    assert checks["EC02_REAL_PLATFORM"]["status"] == "PASS"
    assert "EC02_REAL_PLATFORM" not in payload["release_blocking_keys"]


def test_runtime_verifier_is_source_bound_and_checks_all_service_layers():
    text = (ROOT / "deploy/production_runtime_verify.py").read_text(encoding="utf-8")
    for token in [
        "PRODUCTION_DEPLOYMENT_RUNTIME", "BACKEND_HEALTH", "FRONTEND_AND_API_PROXY",
        "POSTGRES_MIGRATION", "REDIS", "MINIO_READ_WRITE", "CELERY_QUEUES", "PRODUCTION_CONFIG",
        "evidence_envelope",
    ]:
        assert token in text

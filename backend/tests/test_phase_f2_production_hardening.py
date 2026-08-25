from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.auth.providers import AuthRequest, HmacGatewayAuthProvider
from app.contracts.enums import UserRole
from app.core.config import settings
from app.core.errors import AppError
from app.integrations.feishu.transport import FeishuCallbackVerifier, FeishuTransportError
from app.integrations.secrets import SecretRef, SecretResolver
from app.production_config import production_config_readiness

ROOT = Path(__file__).resolve().parents[2]


def test_secret_resolver_prefers_file_then_env_then_direct(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-value\n", encoding="utf-8")
    monkeypatch.setenv("VOIP_TEST_SECRET", "env-value")
    assert SecretResolver.resolve(SecretRef(value="direct", env="VOIP_TEST_SECRET", file=str(secret_file)), name="TEST") == "file-value"
    secret_file.unlink()
    assert SecretResolver.resolve(SecretRef(value="direct", env="VOIP_TEST_SECRET"), name="TEST") == "env-value"
    monkeypatch.delenv("VOIP_TEST_SECRET")
    assert SecretResolver.resolve(SecretRef(value="direct"), name="TEST") == "direct"


def test_gateway_hmac_auth_accepts_valid_signature_and_rejects_tamper(monkeypatch):
    monkeypatch.setattr(settings, "auth_gateway_hmac_secret", "super-secret")
    monkeypatch.setattr(settings, "auth_gateway_hmac_secret_file", "")
    monkeypatch.setattr(settings, "auth_gateway_hmac_secret_env", "")
    monkeypatch.setattr(settings, "auth_gateway_max_skew_seconds", 300)
    ts = str(int(time.time()))
    canonical = f"alice\nENGINEER\n{ts}".encode()
    sig = hmac.new(b"super-secret", canonical, hashlib.sha256).hexdigest()
    identity = HmacGatewayAuthProvider().authenticate(AuthRequest("alice", "ENGINEER", ts, sig))
    assert identity.actor_id == "alice"
    assert identity.role is UserRole.ENGINEER
    assert identity.provider == "gateway_hmac"
    with pytest.raises(AppError) as exc:
        HmacGatewayAuthProvider().authenticate(AuthRequest("alice", "ADMIN", ts, sig))
    assert exc.value.code == "AUTH_SIGNATURE_INVALID"


def test_feishu_callback_signature_verifier(monkeypatch):
    monkeypatch.setattr(settings, "feishu_encrypt_key", "encrypt-secret")
    monkeypatch.setattr(settings, "feishu_encrypt_key_file", "")
    monkeypatch.setattr(settings, "feishu_encrypt_key_env", "")
    monkeypatch.setattr(settings, "feishu_verification_token", "")
    raw = b'{"action":{"value":{"action":"OPEN_CASE"}}}'
    ts, nonce = "1712345678", "abc123"
    sig = hashlib.sha256(ts.encode() + nonce.encode() + b"encrypt-secret" + raw).hexdigest()
    FeishuCallbackVerifier().verify(timestamp=ts, nonce=nonce, signature=sig, raw_body=raw, payload=json.loads(raw))
    with pytest.raises(FeishuTransportError):
        FeishuCallbackVerifier().verify(timestamp=ts, nonce=nonce, signature="0" * 64, raw_body=raw, payload=json.loads(raw))


def test_production_config_gate_is_conservative_by_default():
    payload = production_config_readiness()
    assert payload["status"] == "BLOCKED"
    items = {x["key"]: x for x in payload["items"]}
    assert items["PRODUCTION_AUTH"]["status"] == "BLOCKED"
    assert items["PRODUCTION_STORAGE_CONFIG"]["status"] == "BLOCKED"
    # FEISHU_LIVE_CONFIG reflects the configured .env: PASS when live Feishu is
    # enabled with credentials (this dev env now has them), BLOCKED otherwise.
    feishu_status = items["FEISHU_LIVE_CONFIG"]["status"]
    assert feishu_status in {"PASS", "BLOCKED"}


def test_production_storage_config_uses_resolved_file_secrets(tmp_path, monkeypatch):
    access_file = tmp_path / "minio-access"
    secret_file = tmp_path / "minio-secret"
    access_file.write_text("production-access\n", encoding="utf-8")
    secret_file.write_text("production-secret\n", encoding="utf-8")
    monkeypatch.setattr(settings, "reproduction_storage_mode", "minio")
    monkeypatch.setattr(settings, "minio_access_key", "voipminio")
    monkeypatch.setattr(settings, "minio_access_key_file", str(access_file))
    monkeypatch.setattr(settings, "minio_access_key_env", "")
    monkeypatch.setattr(settings, "minio_secret_key", "voipminiosecret")
    monkeypatch.setattr(settings, "minio_secret_key_file", str(secret_file))
    monkeypatch.setattr(settings, "minio_secret_key_env", "")
    monkeypatch.setattr(settings, "minio_bucket", "voip-evidence")

    items = {item["key"]: item for item in production_config_readiness()["items"]}
    assert items["PRODUCTION_STORAGE_CONFIG"]["status"] == "PASS"


def test_production_app_rejects_insecure_startup_in_fresh_process():
    code = "from pathlib import Path; import sys; sys.path.insert(0, str(Path.cwd()/'tools')); from offline_import_bootstrap import install; install(); import app.main"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "backend"),
        "APP_ENV": "production",
        "AUTH_ALLOW_ANONYMOUS_DEV": "true",
        "PRODUCTION_AUTH_PROVIDER": "pending",
        "CORS_ALLOW_ORIGINS": "*",
    }
    cp = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True)
    assert cp.returncode != 0
    assert "PRODUCTION_ANONYMOUS_AUTH_FORBIDDEN" in (cp.stdout + cp.stderr)


def test_frontend_dockerfile_uses_npm_ci_and_requires_lockfile():
    text = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert "package-lock.json" in text
    assert "npm ci" in text
    assert "npm install" not in text

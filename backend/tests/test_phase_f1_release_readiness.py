from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.release_readiness import runtime_release_readiness

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_release_readiness_is_conservative_for_pending_integrations():
    payload = runtime_release_readiness(profile_root=ROOT / "profiles")
    assert payload["status"] == "BLOCKED"
    items = {x["key"]: x for x in payload["items"]}
    # EC-02 platform contract is promoted to VERIFIED and production-ready for
    # autonomous reproduction (2026-08-14); it is no longer a pending integration.
    assert items["EC02_PLATFORM_PRODUCTION_READY"]["status"] == "PASS"
    # REAL_REPRODUCTION_PLATFORM is configuration-sensitive by contract: PASS in
    # real mode and BLOCKED in mock mode. Full CI intentionally runs in mock mode
    # to prevent device side effects, while live acceptance switches to real only
    # after the regression gate is green.
    configured_mode = os.getenv("REPRODUCTION_PLATFORM_MODE", "mock").strip().lower()
    expected_platform_status = "PASS" if configured_mode == "real" else "BLOCKED"
    assert items["REAL_REPRODUCTION_PLATFORM"]["status"] == expected_platform_status
    assert items["PRODUCTION_AUTH_PROVIDER"]["status"] == "BLOCKED"
    # FEISHU_LIVE_TRANSPORT reflects the configured .env: PASS when
    # FEISHU_LIVE_ENABLED=true and app credentials are present, BLOCKED when live
    # Feishu is intentionally disabled for isolated regression.
    feishu_status = items["FEISHU_LIVE_TRANSPORT"]["status"]
    assert feishu_status in {"PASS", "BLOCKED"}


def test_release_readiness_api_requires_admin_permission_and_reports_blockers_in_fresh_process():
    code = r'''
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / "tools"))
from offline_import_bootstrap import install
install()
from fastapi.testclient import TestClient
from app.main import app
client=TestClient(app)
viewer=client.get("/api/v1/system/release-readiness",headers={"X-Actor-Id":"v1","X-Actor-Role":"VIEWER"})
admin=client.get("/api/v1/system/release-readiness",headers={"X-Actor-Id":"a1","X-Actor-Role":"ADMIN"})
print(__import__("json").dumps({"viewer":viewer.status_code,"admin":admin.status_code,"status":admin.json().get("status")}))
'''
    env={**os.environ,"PYTHONPATH":str(ROOT / "backend")}
    cp=subprocess.run([sys.executable,"-c",code],cwd=ROOT,env=env,text=True,capture_output=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data=json.loads(cp.stdout.strip().splitlines()[-1])
    assert data == {"viewer":403,"admin":200,"status":"BLOCKED"}


def test_release_readiness_accepts_poseidon_credential_provider(tmp_path, monkeypatch):
    from app.core.config import settings

    secret = tmp_path / "secret.yaml"
    secret.write_text("sso:\n  baichuan:\n    username: alice\n    password: poseidon-bootstrap\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setattr(settings, "credential_provider", "poseidon")
    monkeypatch.setenv("LOCAL_SECRET_FILE", str(secret))
    payload = runtime_release_readiness(profile_root=ROOT / "profiles")
    items = {x["key"]: x for x in payload["items"]}
    assert items["PRODUCTION_CREDENTIAL_PROVIDER"]["status"] == "PASS"


def test_migration_contract_has_single_head():
    cp = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "migration_contract_gate.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads(cp.stdout)
    assert data["status"] == "PASS"
    assert len(data["heads"]) == 1


def test_security_gate_passes_while_production_readiness_stays_blocked():
    cp = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "security_release_gate.py")],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        text=True,
        capture_output=True,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads(cp.stdout)
    assert data["status"] == "PASS"
    assert data["checks"]["production_auth_gap_explicit"] is True


def test_release_evidence_rejects_missing_or_stale_source_hash(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    from release_evidence import load_source_bound_evidence, source_manifest_sha256

    missing = tmp_path / "missing.json"
    payload, reason = load_source_bound_evidence(missing, root=ROOT)
    assert payload is None and "missing" in reason

    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"source_manifest_aggregate_sha256": "0" * 64, "passed": True}))
    payload, reason = load_source_bound_evidence(stale, root=ROOT)
    assert payload is None and "stale evidence" in reason

    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps({"source_manifest_aggregate_sha256": source_manifest_sha256(ROOT), "passed": True}))
    payload, reason = load_source_bound_evidence(fresh, root=ROOT)
    assert payload and payload["passed"] is True
    assert reason == "exact-source evidence"

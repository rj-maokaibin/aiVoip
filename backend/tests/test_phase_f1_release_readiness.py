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
    # REAL_REPRODUCTION_PLATFORM stays BLOCKED while REPRODUCTION_PLATFORM_MODE=mock
    # (default in non-production test env); switching to real is the production step.
    assert items["REAL_REPRODUCTION_PLATFORM"]["status"] == "BLOCKED"
    assert items["PRODUCTION_AUTH_PROVIDER"]["status"] == "BLOCKED"
    assert items["FEISHU_LIVE_TRANSPORT"]["status"] == "BLOCKED"


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

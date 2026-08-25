from __future__ import annotations

import json
import os
from pathlib import Path

from app.capture_v2.control import production_feishu_rbac_enable_guarded as guarded


def _repo_with_authorization_and_gate(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    auth = repo / "validation/capture_v2/PRODUCTION_CUTOVER_AUTHORIZATION_RC69.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "authorized": True,
                "cutover_ready": True,
                "technical_release_validation": "PASS",
                "final_acceptance_action": "RC68-MASTER-FIX-CANDIDATE-INTEGRATION-005",
                "final_acceptance_verdict": "PASS",
            }
        ),
        encoding="utf-8",
    )
    gate = repo / "validation/capture_v2_release_gate.json"
    gate.write_text(
        json.dumps(
            {
                "schema_version": "capture-v2-release-gate-v1",
                "software_gate_passed": True,
                "real_ownership_gate_passed": True,
                "real_segment_gate_passed": True,
                "readiness_gate_passed": True,
                "coverage_gate_passed": True,
                "e2e_gate_passed": True,
                "rollback_gate_passed": True,
                "approved": True,
                "production_cutover_approved": True,
                "capture_engine_version": "V1",
                "production_v2_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return repo, auth


def test_guarded_enable_changes_only_feishu_rbac_key(tmp_path: Path, monkeypatch) -> None:
    repo, auth = _repo_with_authorization_and_gate(tmp_path)
    env = tmp_path / "production.env"
    env.write_text(
        "APP_ENV=production\n"
        "CAPTURE_ENGINE_VERSION=V1\n"
        "CAPTURE_V2_PRODUCTION_ENABLED=false\n"
        "REPRODUCTION_PLATFORM_MODE=mock\n"
        "POSTGRES_PASSWORD=do-not-touch\n"
        "FEISHU_IDENTITY_RBAC_ENABLED=false\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o600)
    monkeypatch.setattr(guarded, "PRODUCTION_ENV", env)

    rc, payload = guarded.run(repo_root=repo, authorization_path=auth)

    assert rc == 0
    assert payload["verdict"] == "PASS"
    assert payload["reason"] == "PRODUCTION_FEISHU_RBAC_ENABLED"
    assert payload["mutations_performed"] is True
    assert payload["runtime_restart_performed"] is False
    assert payload["changed_keys"] == ["FEISHU_IDENTITY_RBAC_ENABLED"]
    text = env.read_text(encoding="utf-8")
    assert "FEISHU_IDENTITY_RBAC_ENABLED=true" in text
    assert "POSTGRES_PASSWORD=do-not-touch" in text
    assert "CAPTURE_ENGINE_VERSION=V1" in text
    assert "CAPTURE_V2_PRODUCTION_ENABLED=false" in text
    assert "REPRODUCTION_PLATFORM_MODE=mock" in text
    assert Path(payload["backup_path"]).is_file()


def test_guarded_enable_rejects_non_private_production_env(tmp_path: Path, monkeypatch) -> None:
    repo, auth = _repo_with_authorization_and_gate(tmp_path)
    env = tmp_path / "production.env"
    env.write_text(
        "APP_ENV=production\n"
        "CAPTURE_ENGINE_VERSION=V1\n"
        "CAPTURE_V2_PRODUCTION_ENABLED=false\n"
        "REPRODUCTION_PLATFORM_MODE=mock\n"
        "FEISHU_IDENTITY_RBAC_ENABLED=false\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o644)
    monkeypatch.setattr(guarded, "PRODUCTION_ENV", env)

    rc, payload = guarded.run(repo_root=repo, authorization_path=auth)

    assert rc == 1
    assert payload["verdict"] == "FAIL"
    assert payload["reason"] == "PRODUCTION_ENV_PERMISSIONS_NOT_PRIVATE"
    assert payload["mutations_performed"] is False
    assert "FEISHU_IDENTITY_RBAC_ENABLED=false" in env.read_text(encoding="utf-8")

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_deploy_cli_manages_feishu_long_connection_end_to_end():
    script = _text("deploy/voip-ai")
    assert "feishu-long-connection" in script
    assert "wait_feishu_long_connection" in script
    assert "verify_feishu_consumer_host" in script
    assert "FEISHU_LONG_CONNECTION_HOST_GATE=PASS" in script
    assert "consumer_count': 1" in script
    assert "validation/feishu_long_connection_runtime.json" in script


def test_production_compose_gives_feishu_listener_restart_secrets_and_healthcheck():
    compose = yaml.safe_load(_text("docker-compose.production.yml"))
    service = compose["services"]["feishu-long-connection"]
    assert service["restart"] == "unless-stopped"
    assert set(service["secrets"]) == {"feishu_app_secret", "feishu_verification_token"}
    assert service["healthcheck"]["test"][0] == "CMD"
    assert "run_feishu_long_connection.py" in service["healthcheck"]["test"][-1]


def test_feishu_listener_entrypoint_is_source_manifest_bound():
    gate = _text("tools/source_manifest_gate.py")
    assert '"backend/run_feishu_long_connection.py"' in gate


def test_celery_cannot_register_legacy_feishu_long_connection_consumer():
    celery_app = _text("backend/app/workers/celery_app.py")
    assert "feishu_long_connection_task" not in celery_app
    assert "feishu.long_connection" not in celery_app

    legacy = _text("backend/app/workers/feishu_long_connection_task.py")
    assert "celery_app.task" not in legacy
    assert "run_long_connection(" not in legacy
    assert "LEGACY_FEISHU_LONG_CONNECTION_REMOVED" in legacy

    compose = yaml.safe_load(_text("docker-compose.yml"))
    service = compose["services"]["feishu-long-connection"]
    assert service["command"] == "python run_feishu_long_connection.py"


def test_long_connection_resolves_app_secret_from_mounted_file(tmp_path, monkeypatch):
    from app.integrations.feishu import long_connection

    secret = tmp_path / "feishu_app_secret"
    secret.write_text("mounted-secret\n", encoding="utf-8")
    monkeypatch.setattr(long_connection.settings, "feishu_app_secret", "")
    monkeypatch.setattr(long_connection.settings, "feishu_app_secret_file", str(secret))
    monkeypatch.setattr(long_connection.settings, "feishu_app_secret_env", "")

    assert long_connection._app_secret() == "mounted-secret"


def test_listener_entrypoint_returns_nonzero_when_live_transport_is_disabled(monkeypatch):
    import run_feishu_long_connection as entrypoint

    monkeypatch.setattr(entrypoint.settings, "feishu_live_enabled", False)
    assert entrypoint.main() != 0

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
    release_runner_volumes = set(payload["services"]["release-runner"]["volumes"])
    assert "./validation:/app/validation:ro" in release_runner_volumes


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
    assert '"text/html" not in content_type' in text
    assert "'<html'" not in text


def _run_preflight_with_fake_docker(tmp_path: Path, info_stderr: str = "", info_rc: int = 0) -> subprocess.CompletedProcess:
    """Run deploy/voip-ai preflight with a fake docker in PATH.

    The fake docker passes `compose version` (so the compose plugin gate is
    satisfied) and then reports the given `docker info` failure, letting the
    test assert that require_docker classifies the real cause instead of
    collapsing every failure into "Docker daemon is not reachable".
    """
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"compose\" ]]; then exit 0; fi\n"
        "if [[ -n \"$FAKE_INFO_STDERR\" ]]; then printf '%s\\n' \"$FAKE_INFO_STDERR\" >&2; fi\n"
        "exit \"$FAKE_INFO_RC\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_INFO_STDERR"] = info_stderr
    env["FAKE_INFO_RC"] = str(info_rc)
    return subprocess.run(
        ["bash", str(ROOT / "deploy/voip-ai"), "--env", str(tmp_path / "missing.env"), "preflight"],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )


def test_preflight_classifies_docker_permission_denied(tmp_path):
    cp = _run_preflight_with_fake_docker(
        tmp_path,
        info_stderr=(
            "permission denied while trying to connect to the docker daemon socket "
            "at unix:///var/run/docker.sock: dial unix /var/run/docker.sock: "
            "connect: permission denied"
        ),
        info_rc=1,
    )
    assert cp.returncode == 126
    assert "Docker permission denied" in cp.stderr
    assert "Docker daemon is not reachable" not in cp.stderr


def test_preflight_classifies_docker_daemon_down(tmp_path):
    cp = _run_preflight_with_fake_docker(
        tmp_path,
        info_stderr=(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?"
        ),
        info_rc=1,
    )
    assert cp.returncode == 126
    assert "Docker daemon is not reachable" in cp.stderr
    assert "Docker permission denied" not in cp.stderr


def test_preflight_classifies_docker_context_invalid(tmp_path):
    cp = _run_preflight_with_fake_docker(
        tmp_path,
        info_stderr='docker: error during connect: current context is not defined: "bogus"',
        info_rc=1,
    )
    assert cp.returncode == 126
    assert "Docker context is invalid" in cp.stderr


def test_preflight_docker_ok_proceeds_to_env_check(tmp_path):
    cp = _run_preflight_with_fake_docker(tmp_path, info_stderr="", info_rc=0)
    assert cp.returncode == 2
    assert "production env file missing" in cp.stderr
    assert "Docker daemon is not reachable" not in cp.stderr


def _fake_docker_echoing_project(tmp_path: Path) -> Path:
    """Fake docker that echoes the compose --project-name argument."""
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"compose\" ]]; then\n"
        "  prev=\"\"\n"
        "  for a in \"$@\"; do\n"
        "    if [[ \"$prev\" == \"--project-name\" ]]; then echo \"SEEN_PROJECT=$a\"; fi\n"
        "    prev=\"$a\"\n"
        "  done\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_status_with_fake_docker(tmp_path: Path, env_text: str, extra_args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    fake = _fake_docker_echoing_project(tmp_path)
    env_file = tmp_path / "production.env"
    env_file.write_text(env_text, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(ROOT / "deploy/voip-ai"), "--env", str(env_file), *extra_args, "status"],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )


def test_deploy_cli_resolves_project_name_from_env_file(tmp_path):
    cp = _run_status_with_fake_docker(tmp_path, "VOIP_PROJECT_NAME=proj-check\n")
    assert cp.returncode == 0
    assert "SEEN_PROJECT=proj-check" in cp.stdout


def test_deploy_cli_defaults_project_to_production_stack(tmp_path):
    cp = _run_status_with_fake_docker(tmp_path, "")
    assert cp.returncode == 0
    assert "SEEN_PROJECT=aivoip" in cp.stdout


def test_deploy_cli_project_flag_overrides_env_file(tmp_path):
    cp = _run_status_with_fake_docker(tmp_path, "VOIP_PROJECT_NAME=proj-check\n", extra_args=("--project", "cli-proj"))
    assert cp.returncode == 0
    assert "SEEN_PROJECT=cli-proj" in cp.stdout

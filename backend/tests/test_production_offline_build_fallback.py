from __future__ import annotations

import subprocess
from pathlib import Path

from deploy.offline_build_fallback import build_audit, registry_network_failure


def test_registry_network_failure_requires_registry_and_network_markers():
    log = '''
    load metadata for docker.io/library/python:3.12-slim
    failed to resolve source metadata: failed to do request: Head
    "http://mirror.invalid/v2/library/python/manifests/3.12-slim": no route to host
    '''
    assert registry_network_failure(log) is True


def test_dependency_download_timeout_does_not_authorize_offline_fallback():
    assert registry_network_failure("RUN pip install: i/o timeout") is False


def test_registry_auth_or_manifest_error_does_not_authorize_fallback():
    log = "failed to resolve source metadata: pull access denied"
    assert registry_network_failure(log) is False


def _runner(missing: set[str]):
    def run(command, **_kwargs):
        image = command[3]
        if image in missing:
            return subprocess.CompletedProcess(command, 1, "", "No such image")
        return subprocess.CompletedProcess(command, 0, f"sha256:{image}\n", "")
    return run


def test_audit_allows_only_when_every_required_image_exists():
    log = "failed to resolve reference /v2/library/python/manifests/3.12-slim: no route to host"
    payload = build_audit(log, online_exit_code=1,
                          images=("python:3.12-slim", "node:22-alpine"), run=_runner(set()))
    assert payload["status"] == "ALLOWED"
    assert payload["reason"] == "REGISTRY_NETWORK_FAILURE_AND_LOCAL_IMAGES_COMPLETE"
    assert payload["local_image_inventory_complete"] is True


def test_audit_fails_closed_when_one_required_image_is_missing():
    log = "failed to resolve reference /v2/library/python/manifests/3.12-slim: network is unreachable"
    payload = build_audit(log, online_exit_code=1,
                          images=("python:3.12-slim", "node:22-alpine"),
                          run=_runner({"node:22-alpine"}))
    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "REQUIRED_LOCAL_IMAGE_MISSING"
    assert payload["fallback"] is None


def test_production_cli_prefers_pull_and_uses_guarded_fallback():
    text = (Path(__file__).resolve().parents[2] / "deploy/voip-ai").read_text(encoding="utf-8")
    online = text.index('compose build --pull "${services[@]}"')
    guard = text.index("offline_build_fallback.py")
    offline = text.index('compose build --pull=false "${services[@]}"')
    assert online < guard < offline
    assert "VOIP_OFFLINE_BUILD_AUDIT" in text

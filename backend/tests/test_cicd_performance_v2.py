from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_revision_label_does_not_invalidate_dependency_layers():
    text = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert text.index("pip install -r requirements.txt") < text.index("ARG BUILD_REVISION=unknown")
    assert "--mount=type=cache,target=/root/.cache/pip" in text


def test_frontend_npm_install_isolated_from_source_copy():
    text = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")
    assert text.index("npm ci --no-audit --no-fund") < text.index("COPY src ./src")
    assert "--mount=type=cache,target=/root/.npm" in text


def test_production_deploy_records_timing_and_keeps_governance():
    text = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    assert "CICD_PERFORMANCE_V2_EVIDENCE" in text
    assert "perf_phase runtime_verify verify_stack" in text
    assert "source_binding_preflight" in text
    assert "python3 tools/source_manifest_gate.py" in text
    assert "compose config >/dev/null" in text


def test_registry_probe_fails_closed_or_uses_audited_fallback():
    text = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    probe = text.index("REGISTRY_PREFLIGHT=FAIL")
    guard = text.index("offline_build_fallback.py", probe)
    offline = text.index("compose build --pull=false", guard)
    assert probe < guard < offline
    assert "VOIP_REGISTRY_PROBE_TIMEOUT_SECONDS" in text


def test_self_hosted_pr_gates_use_immutable_source_bundle():
    for rel in (
        ".github/workflows/source-manifest-gate.yml",
        ".github/workflows/prd-spec-v1-release.yml",
        ".github/workflows/preliminary-evidence-v1.yml",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "exact-source-bundle:" in text
        assert "runs-on: ubuntu-latest" in text
        assert "fetch-depth: 0" in text
        assert "git bundle create" in text
        assert "actions/download-artifact@v4" in text
        assert "EXACT_SOURCE_MATERIALIZATION=PASS" in text
        assert "EXPECTED_SHA" in text
        assert "voip-controlled-linux" in text


def test_production_offline_build_is_explicitly_fail_closed():
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    assert deploy.count('offline_rc="$?"') == 2
    assert "PRODUCTION_IMAGE_BUILD=FAIL mode=OFFLINE_LOCAL_INVENTORY source=REGISTRY_PREFLIGHT" in deploy
    assert "PRODUCTION_IMAGE_BUILD=FAIL mode=OFFLINE_LOCAL_INVENTORY source=POSTBUILD_FALLBACK" in deploy
    assert "verify_feishu_consumer_host || return $?" in deploy
    assert "--out validation/exact_source_binding_result.json || return $?" in deploy


def test_offline_build_has_no_external_dockerfile_frontend_dependency():
    for rel in ("backend/Dockerfile", "frontend/Dockerfile"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not text.startswith("# syntax=docker/dockerfile:")
        assert "RUN --mount=type=cache" in text


def test_production_workflow_repairs_workspace_before_checkout():
    text = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    assert text.index("Repair self-hosted workspace before checkout") < text.index("Checkout exact master")
    assert "PRODUCTION_RUNNER_WORKSPACE_REPAIR=PASS" in text


def test_production_network_is_named_narrow_guarded_and_idempotent():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    guard = (ROOT / "deploy/docker_network_guard.py").read_text(encoding="utf-8")
    assert "VOIP_DOCKER_NETWORK_NAME:-aivoip-production" in compose
    assert "VOIP_DOCKER_SUBNET:-172.30.250.0/24" in compose
    assert "docker_network_guard.py prepare" in deploy
    assert "docker_network_guard.py cleanup" in deploy
    assert "DESIRED_SUBNET_CONTAINS_REGISTRY_MIRROR" in guard
    assert "LEGACY_CONFLICT_NETWORK_STILL_IN_USE" in guard
    assert "EXISTING_PRODUCTION_NETWORK_SUBNET_MISMATCH" in guard
    assert "PRODUCTION_NETWORK_NOT_MATERIALIZED_AS_EXPECTED" in guard
    assert "n['name'] == network_name and subnet == desired" in guard
    assert "created_by_guard" in guard

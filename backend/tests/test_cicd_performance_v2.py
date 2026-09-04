from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_revision_label_does_not_invalidate_dependency_layers():
    text = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    requirements_copy = text.index("COPY requirements.txt .")
    pip_cache_layer = text.index("RUN --mount=type=cache,target=/root/.cache/pip", requirements_copy)
    source_copy = text.index("COPY . .", pip_cache_layer)
    revision = text.index("ARG BUILD_REVISION=unknown", source_copy)
    assert requirements_copy < pip_cache_layer < source_copy < revision
    assert "pip install" in text[pip_cache_layer:source_copy]
    assert "--mount=type=cache,target=/root/.cache/pip" in text[pip_cache_layer:source_copy]


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


def test_v2_2_pr_gates_share_exact_source_and_only_authoritative_acceptance_uses_self_hosted():
    manifest = (ROOT / ".github/workflows/source-manifest-gate.yml").read_text(encoding="utf-8")
    full = (ROOT / ".github/workflows/prd-spec-v1-release.yml").read_text(encoding="utf-8")
    preliminary = (ROOT / ".github/workflows/preliminary-evidence-v1.yml").read_text(encoding="utf-8")

    assert "exact-source-bundle:" in manifest
    assert "Upload shared immutable source bundle" in manifest
    assert "exact-source-${{ env.EXPECTED_SHA }}" in manifest
    assert manifest.count("runs-on: ubuntu-latest") >= 2
    assert "voip-controlled-linux" not in manifest
    assert "EXACT_SOURCE_MATERIALIZATION=PASS" in manifest

    assert "resolve-shared-source:" in full
    assert "Wait for Source Manifest Gate exact-SHA bundle" in full
    assert "Download shared immutable source bundle" in full
    assert "run-id: ${{ needs.resolve-shared-source.outputs.run_id }}" in full
    assert "EXACT_SOURCE_MATERIALIZATION=PASS" in full
    assert "voip-controlled-linux" in full
    assert "Full VOIP AI software release gate" in full
    assert "Prepared-PCAP Real Offline Golden 001" in full
    assert "Real Offline Golden 001 Human Evidence Gate" in full
    assert "full-acceptance-${{ env.EXPECTED_SHA }}" in full

    assert "verify-full-acceptance-evidence:" in preliminary
    assert "runs-on: ubuntu-latest" in preliminary
    assert "voip-controlled-linux" not in preliminary
    assert "PRELIMINARY_REUSED_FULL_ACCEPTANCE=PASS" in preliminary
    assert "full-acceptance-${{ env.EXPECTED_SHA }}" in preliminary
    assert "bash tools/voip_ai_release_gate.sh" not in preliminary
    assert "offline_analysis_golden_replay.py" not in preliminary


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


def test_v2_2_production_repairs_workspace_then_materializes_immutable_source_offline():
    text = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    repair = text.index("Repair self-hosted workspace before materialization")
    download = text.index("Download immutable production source")
    materialize = text.index("Materialize exact master offline")
    self_hosted = text[text.index("deploy-and-verify:"):]
    assert repair < download < materialize
    assert "immutable-source-bundle:" in text
    assert "Checkout exact deployment source on GitHub-hosted transport" in text
    assert "Checkout exact master" not in self_hosted
    assert "uses: actions/checkout@" not in self_hosted
    assert "release-authority:" in text
    assert "Require clean merge commit with accepted-head tree identity" in text
    assert "Require exact accepted PR head gates" in text
    assert "PRODUCTION_PR_AUTHORITY=PASS" in text
    assert "needs: release-authority" in text
    assert "git -C \"$GITHUB_WORKSPACE\" update-ref refs/remotes/origin/master \"$EXPECTED_SHA\"" in text
    assert "PRODUCTION_TARGET_RESOLUTION=PASS source=IMMUTABLE_BUNDLE" in text
    assert "PRODUCTION_SOURCE_TRANSPORT=IMMUTABLE_BUNDLE" in text
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


def test_v2_1_registry_probe_is_transport_only_and_not_full_pull():
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    probe = (ROOT / "deploy/registry_connectivity_probe.py").read_text(encoding="utf-8")
    assert "registry_connectivity_probe.py" in deploy
    assert 'REGISTRY_PREFLIGHT=PASS mode=HTTP_CONNECTIVITY' in deploy
    assert 'docker pull "$probe_image"' not in deploy
    assert "HTTPError" in probe
    assert "REGISTRY_CONNECTIVITY=PASS" in probe


def test_v2_1_backend_runtime_services_share_one_built_image():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prod = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    shared = 'image: aivoip-backend:${BUILD_REVISION:?BUILD_REVISION is required}'
    assert compose.count(shared) >= 11
    assert shared in prod
    assert "local services=(backend frontend)" in deploy


def test_v2_1_production_workflow_restores_workspace_ownership():
    text = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    assert "Restore runner workspace ownership" in text
    assert "PRODUCTION_RUNNER_WORKSPACE_RESTORE=PASS" in text
    assert "validation/registry_connectivity_v2_1.json" in text


def test_v2_2_runtime_build_revision_does_not_mutate_persistent_env(tmp_path):
    import stat
    import subprocess
    import sys

    base = tmp_path / "production.env"
    base.write_text(
        "APP_ENV=production\n"
        "BUILD_REVISION=1111111111111111111111111111111111111111\n"
        "VOIP_PROJECT_NAME=aivoip\n",
        encoding="utf-8",
    )
    base.chmod(0o600)
    before = base.read_bytes()
    out = tmp_path / "runtime.env"
    revision = "a" * 40
    cp = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/runtime_env.py"),
            "--base-env",
            str(base),
            "--revision",
            revision,
            "--out",
            str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert base.read_bytes() == before
    runtime = out.read_text(encoding="utf-8")
    assert runtime.count("BUILD_REVISION=") == 1
    assert f"BUILD_REVISION={revision}" in runtime
    assert "1111111111111111111111111111111111111111" not in runtime
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "persistent_env_mutated=false" in cp.stdout


def test_v2_2_runtime_env_rejects_insecure_base_env(tmp_path):
    import subprocess
    import sys

    base = tmp_path / "production.env"
    base.write_text("APP_ENV=production\n", encoding="utf-8")
    base.chmod(0o644)
    out = tmp_path / "runtime.env"
    cp = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/runtime_env.py"),
            "--base-env",
            str(base),
            "--revision",
            "b" * 40,
            "--out",
            str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert cp.returncode == 2
    assert "group/world accessible" in cp.stderr
    assert not out.exists()


def test_v2_2_cli_and_formal_workflow_use_source_controlled_runtime_revision():
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    template = (ROOT / "deploy/production.env.example").read_text(encoding="utf-8")

    assert "--revision SHA" in deploy
    assert "materialize_runtime_env" in deploy
    assert "python3 deploy/runtime_env.py" in deploy
    assert "persistent_env_mutated=false" in deploy
    assert "requested revision does not match checked-out source" in deploy
    assert 'sudo -n /usr/local/sbin/voip-ai-production-deploy "$TARGET_SHA"' in workflow
    assert "sudo -n ./deploy/voip-ai" not in workflow
    assert "privilege_boundary=HOST_WRAPPER" in workflow
    assert "BUILD_REVISION_SOURCE=RUNTIME revision=$TARGET_SHA" in workflow
    assert "persistent_env_mutated=false" in workflow
    assert "PRODUCTION_PERSISTENT_ENV_UNCHANGED=PASS" in workflow
    assert "runtime_revision_injection.json" in workflow
    assert "-v /etc/voip-ai/production.env:/input:ro" in workflow
    assert "BUILD_REVISION=<immutable-git-sha-or-build-id>" not in template
    assert "do not maintain BUILD_REVISION here" in template

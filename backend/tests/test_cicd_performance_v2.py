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

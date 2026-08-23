from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_production_deploy_starts_full_capture_v2_control_runtime():
    text = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    for service in (
        "reproduction-worker",
        "reproduction-control-high-worker",
        "reproduction-watch-worker",
        "beat",
    ):
        assert service in text

    promotion = text.split('echo "Starting backend, workers, scheduler and frontend..."', 1)[1]
    promotion = promotion.split("local timeout backend_port frontend_port", 1)[0]
    assert "reproduction-worker" in promotion
    assert "reproduction-control-high-worker" in promotion
    assert "reproduction-watch-worker" in promotion
    assert "beat" in promotion


def test_production_runtime_verifier_requires_real_v2_control_queues():
    text = (ROOT / "deploy/production_runtime_verify.py").read_text(encoding="utf-8")
    assert '"reproduction-control"' in text
    assert '"reproduction-control-high"' in text
    assert '"reproduction-watch"' in text
    assert '"reproduction"' not in text.split("required = {", 1)[1].split("}", 1)[0]
    assert 'check("REPRODUCTION_PLATFORM", reproduction_platform)' in text
    assert 'check("CAPTURE_AUTHORITY", capture_authority)' in text


def test_production_cutover_is_guarded_and_rolls_back_failures():
    text = (ROOT / "backend/app/capture_v2/control/production_cutover_guarded.py").read_text(encoding="utf-8")
    assert '"REPRODUCTION_PLATFORM_MODE":"real"' in text
    assert '"CAPTURE_ENGINE_VERSION":"V2"' in text
    assert '"CAPTURE_V2_PRODUCTION_ENABLED":"true"' in text
    assert "production.env.capture-v2-pre-" in text
    assert "_rollback(repo_root" in text
    assert "_restore_source_manifest(repo_root, source_manifest_original)" in text
    assert "PRODUCTION_CUTOVER_FAILED_ROLLBACK_UNVERIFIED" in text


def test_master_fix_candidate_fetch_does_not_depend_on_shared_fetch_head():
    text = (ROOT / "backend/app/capture_v2/control/master_fix_candidate_regression.py").read_text(encoding="utf-8")
    assert "candidate_local_ref" in text
    assert 'f"{CANDIDATE_REF}:{candidate_local_ref}"' in text
    assert '_git(repo_root, "rev-parse", candidate_local_ref)' in text
    assert '_git(repo_root, "rev-parse", "FETCH_HEAD")' not in text

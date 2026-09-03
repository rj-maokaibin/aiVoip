from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_production_source_artifact_is_reusable_across_rerun_attempts():
    workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    source_name = "production-exact-source-${{ github.run_id }}"

    assert workflow.count(source_name) == 2
    assert "production-exact-source-${{ github.run_id }}-${{ github.run_attempt }}" not in workflow
    assert "overwrite: true" in workflow
    assert "test \"$(cat \"$sha_file\")\" = \"$EXPECTED_SHA\"" in workflow
    assert "PRODUCTION_TARGET_RESOLUTION=PASS source=IMMUTABLE_BUNDLE" in workflow

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_production_source_artifact_is_reusable_across_rerun_attempts():
    workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    transport = (ROOT / "tools/exact_tree_transport.py").read_text(encoding="utf-8")
    source_name = "production-exact-source-${{ github.run_id }}"

    assert workflow.count(source_name) == 2
    assert "production-exact-source-${{ github.run_id }}-${{ github.run_attempt }}" not in workflow
    assert "overwrite: true" in workflow
    assert "test \"$(cat \"$sha_file\")\" = \"$EXPECTED_SHA\"" in workflow
    assert "source.identity.json" in workflow
    assert "source.pack" in workflow
    assert "source.paths" in workflow
    assert "transport_tool.py" in workflow
    assert "pack_sha256" in transport
    assert "sparse_paths_sha256" in transport
    assert "transport_tool_sha256" in transport
    assert "PRODUCTION_TARGET_RESOLUTION=PASS source=EXACT_COMMIT_SPARSE_OBJECT_PACK" in workflow

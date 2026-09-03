from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_production_acceptance_persists_result_to_shared_validation_mount():
    script = (ROOT / "tools/evidence_v2_production_acceptance.py").read_text(encoding="utf-8")
    assert 'SHARED_RESULT_PATH = Path("/validation/evidence_v2_production_acceptance.json")' in script
    assert "_persist_result(args.result, payload)" in script
    assert "return 0 if payload.get(\"status\") == \"PASS\" else 2" in script


def test_shared_result_persistence_does_not_replace_requested_result():
    script = (ROOT / "tools/evidence_v2_production_acceptance.py").read_text(encoding="utf-8")
    requested_write = 'result_path.write_text(text, encoding="utf-8")'
    shared_write = 'SHARED_RESULT_PATH.write_text(text, encoding="utf-8")'
    assert requested_write in script
    assert shared_write in script
    assert script.index(requested_write) < script.index(shared_write)

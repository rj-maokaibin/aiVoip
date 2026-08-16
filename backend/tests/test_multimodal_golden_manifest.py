import json
from pathlib import Path


def test_multimodal_golden_manifest_is_a_complete_development_gate():
    manifest_path = Path(__file__).resolve().parents[2] / "golden_cases" / "multimodal_field_v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "multimodal-field-golden-v1"
    assert manifest["status"] == "SYNTHETIC_DEVELOPMENT_BASELINE"
    assert manifest["field_sample_required"] is True

    cases = manifest["cases"]
    assert len(cases) == 8
    assert {case["id"] for case in cases} == {f"MM-{index:03d}" for index in range(1, 9)}
    assert all(case["name"] and case["inputs"] and case["expected"] for case in cases)

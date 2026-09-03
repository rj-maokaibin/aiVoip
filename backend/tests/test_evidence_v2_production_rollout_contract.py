import json
from pathlib import Path

from app.reports.v2.migration import rollout_from_env


ROOT = Path(__file__).resolve().parents[2]


def test_source_controlled_rollout_stage_matches_backend_image_defaults():
    policy = json.loads((ROOT / "deploy/evidence_v2_rollout.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    stage = policy["stage"]
    assert policy["strict_validator"] is True
    assert stage in {"SHADOW", "CANARY", "DEFAULT"}
    assert "PRELIMINARY_EVIDENCE_V2_COMPOSE=true" in dockerfile
    assert "PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true" in dockerfile
    expected_project = "true" if stage == "DEFAULT" else "false"
    assert f"PRELIMINARY_EVIDENCE_V2_PROJECT={expected_project}" in dockerfile


def test_rollout_contract_modes_for_production_stages():
    shadow = rollout_from_env({
        "PRELIMINARY_EVIDENCE_V2_COMPOSE": "true",
        "PRELIMINARY_EVIDENCE_V2_PROJECT": "false",
        "PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR": "true",
    })
    assert shadow.mode == "SHADOW"

    v2 = rollout_from_env({
        "PRELIMINARY_EVIDENCE_V2_COMPOSE": "true",
        "PRELIMINARY_EVIDENCE_V2_PROJECT": "true",
        "PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR": "true",
    })
    assert v2.mode == "V2"
    assert v2.strict_validator is True

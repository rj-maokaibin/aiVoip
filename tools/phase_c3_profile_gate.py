#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.enums import ConfirmationPolicy, ExperimentVariant
from app.experiments.profile import ExperimentProfileRegistry
from app.reproduction.question_graph import DiagnosticQuestionRegistry
from app.reproduction.profile import ReproductionProfileRegistry

EXPECTED_EXPERIMENTS = {
    "PHONE_SWAP_AB",
    "LINE_SWAP_AB",
    "FXS_PORT_SWAP_AB",
    "POWER_SUPPLY_AB",
    "DEVICE_SWAP_AB",
    "POST_REBOOT_FIRST_CALL",
}
FORBIDDEN_EXECUTION_KEYS = {"command", "command_template", "shell", "ssh_command", "aim_command", "action_id"}


def _walk_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k).lower()
            yield from _walk_keys(v)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def main() -> None:
    questions = DiagnosticQuestionRegistry(ROOT / "profiles" / "questions")
    experiments = ExperimentProfileRegistry(ROOT / "profiles" / "experiments")
    reproduction = ReproductionProfileRegistry(ROOT / "profiles")

    exp_map = {x.definition.id: x for x in experiments.list()}
    assert set(exp_map) == EXPECTED_EXPERIMENTS, (set(exp_map), EXPECTED_EXPERIMENTS)
    assert len(questions.list()) == 17

    repro_ids = {x.definition.id for x in reproduction.list()}
    for loaded in exp_map.values():
        d = loaded.definition
        assert d.reproduction_profile_id in repro_ids
        assert d.independent_variable in d.expected_change_paths
        assert not (set(d.expected_change_paths) & set(d.must_equal_paths))
        keys = set(_walk_keys(d.canonical()))
        assert not (keys & FORBIDDEN_EXECUTION_KEYS), (d.id, keys & FORBIDDEN_EXECUTION_KEYS)
        if d.confirmation_policy in {ConfirmationPolicy.ABA_REQUIRED, ConfirmationPolicy.ABA_PREFERRED}:
            assert {ExperimentVariant.A1, ExperimentVariant.B, ExperimentVariant.A2}.issubset(set(d.sequence))
        if d.id == "POST_REBOOT_FIRST_CALL":
            assert d.external_action_required
            assert d.confirmation_policy == ConfirmationPolicy.REPEAT_MATCH
            assert "REPEAT" in {x.value for x in d.sequence}

    # Question -> Experiment references must be explicit, resolvable, and semantically
    # aligned with the deterministic target finding required by that root-cause question.
    referenced = set()
    for q in questions.list():
        for exp_id in q.experiment_profiles:
            assert exp_id in exp_map, (q.id, exp_id)
            referenced.add(exp_id)
            target = exp_map[exp_id].definition.target_finding
            required = set(q.required_evidence.must_findings)
            assert not required or target in required, (q.id, exp_id, target, required)
    assert referenced == EXPECTED_EXPERIMENTS

    payload = {
        "status": "PASS",
        "diagnostic_questions": len(questions.list()),
        "experiment_profiles": len(exp_map),
        "question_experiment_references": len(referenced),
        "ec02_boundary": "PASS_NO_REAL_COMMANDS",
        "experiment_ids": sorted(exp_map),
    }
    out = ROOT / ".phase-c3-profile-gate.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

from datetime import datetime, timedelta, timezone

from app.capture_v2.gate.models import GateVerdict
from app.capture_v2.gate.r7_rollback import (
    R7RollbackRehearsalGate,
    RollbackActivationMode,
    RollbackEvidenceKind,
    RollbackObservation,
    RollbackPhase,
    main,
)


BASE = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)


def _obs(
    phase: RollbackPhase,
    seconds: int,
    *,
    engine: str,
    enabled: bool,
    v1_healthy: bool,
    v2_count: int,
    mode: RollbackActivationMode | None = None,
    kind: RollbackEvidenceKind = RollbackEvidenceKind.REAL_DUT,
    ref: str | None = None,
) -> RollbackObservation:
    if mode is None:
        mode = (
            RollbackActivationMode.V1
            if engine == "V1"
            else RollbackActivationMode.PRODUCTION
            if enabled
            else RollbackActivationMode.ACTIVATION_REHEARSAL
        )
    return RollbackObservation(
        phase=phase,
        observed_at=BASE + timedelta(seconds=seconds),
        capture_engine_version=engine,
        capture_v2_production_enabled=enabled,
        activation_mode=mode,
        v1_healthy=v1_healthy,
        v2_producer_count=v2_count,
        evidence_kind=kind,
        evidence_refs=(ref or f"evidence://{phase.value.lower()}",),
    )


def _valid_real_rehearsal():
    return (
        _obs(RollbackPhase.PRE_V1, 0, engine="V1", enabled=False, v1_healthy=True, v2_count=0),
        _obs(
            RollbackPhase.V2_ACTIVE, 10,
            engine="V2", enabled=False, v1_healthy=True, v2_count=1,
            mode=RollbackActivationMode.ACTIVATION_REHEARSAL,
        ),
        _obs(RollbackPhase.ROLLED_BACK_V1, 20, engine="V1", enabled=False, v1_healthy=True, v2_count=0),
    )


def _artifact_from(observations):
    return {
        "schema_version": R7RollbackRehearsalGate.EVIDENCE_SCHEMA,
        "observations": [obs.as_dict() for obs in observations],
    }


def test_missing_real_v2_phase_is_deferred_not_pass():
    observations = _valid_real_rehearsal()
    result = R7RollbackRehearsalGate.evaluate((observations[0], observations[2]))
    assert result.verdict == GateVerdict.DEFERRED_REAL_GATE
    assert result.facts["reason"] == "ROLLBACK_REAL_PHASES_MISSING"


def test_simulated_rehearsal_can_never_pass_release_gate():
    pre, _, rolled = _valid_real_rehearsal()
    v2 = _obs(
        RollbackPhase.V2_ACTIVE,
        10,
        engine="V2",
        enabled=False,
        mode=RollbackActivationMode.ACTIVATION_REHEARSAL,
        v1_healthy=True,
        v2_count=1,
        kind=RollbackEvidenceKind.SIMULATED,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.DEFERRED_REAL_GATE
    assert result.facts["reason"] == "ROLLBACK_REAL_DUT_EVIDENCE_REQUIRED"


def test_nonchronological_evidence_is_inconclusive():
    pre, _, rolled = _valid_real_rehearsal()
    v2 = _obs(
        RollbackPhase.V2_ACTIVE, 30,
        engine="V2", enabled=False, v1_healthy=True, v2_count=1,
        mode=RollbackActivationMode.ACTIVATION_REHEARSAL,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_EVIDENCE_TIME_ORDER_INVALID"


def test_reused_evidence_ref_is_inconclusive():
    pre = _obs(RollbackPhase.PRE_V1, 0, engine="V1", enabled=False, v1_healthy=True, v2_count=0, ref="same")
    v2 = _obs(
        RollbackPhase.V2_ACTIVE, 10,
        engine="V2", enabled=False, v1_healthy=True, v2_count=1,
        mode=RollbackActivationMode.ACTIVATION_REHEARSAL, ref="same",
    )
    rolled = _obs(RollbackPhase.ROLLED_BACK_V1, 20, engine="V1", enabled=False, v1_healthy=True, v2_count=0)
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_EVIDENCE_REF_REUSED"


def test_real_observation_with_v1_not_restored_fails():
    pre, v2, _ = _valid_real_rehearsal()
    rolled = _obs(
        RollbackPhase.ROLLED_BACK_V1,
        20,
        engine="V1",
        enabled=False,
        v1_healthy=False,
        v2_count=0,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.FAIL
    assert "rollback_v1_healthy" in result.facts["failed_checks"]


def test_real_observation_with_v2_producer_left_after_rollback_fails():
    pre, v2, _ = _valid_real_rehearsal()
    rolled = _obs(
        RollbackPhase.ROLLED_BACK_V1,
        20,
        engine="V1",
        enabled=False,
        v1_healthy=True,
        v2_count=1,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.FAIL
    assert "rollback_no_v2_producer" in result.facts["failed_checks"]


def test_explicit_real_dut_activation_rehearsal_passes_pre_cutover_gate():
    result = R7RollbackRehearsalGate.evaluate(_valid_real_rehearsal())
    assert result.verdict == GateVerdict.PASS
    assert result.facts["reason"] == "ROLLBACK_REHEARSAL_PROVEN"
    assert result.facts["v2_activation_mode"] == "ACTIVATION_REHEARSAL"
    assert result.facts["release_gate_scope"] == "PRE_CUTOVER_ACTIVATION_REHEARSAL"


def test_explicit_production_v2_observation_is_still_valid_if_available():
    pre, _, rolled = _valid_real_rehearsal()
    v2 = _obs(
        RollbackPhase.V2_ACTIVE, 10,
        engine="V2", enabled=True, v1_healthy=True, v2_count=1,
        mode=RollbackActivationMode.PRODUCTION,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.PASS
    assert result.facts["release_gate_scope"] == "PRODUCTION_ROLLBACK_OBSERVATION"


def test_ambiguous_v2_without_explicit_rehearsal_mode_fails():
    pre, _, rolled = _valid_real_rehearsal()
    v2 = _obs(
        RollbackPhase.V2_ACTIVE, 10,
        engine="V2", enabled=False, v1_healthy=True, v2_count=1,
        mode=RollbackActivationMode.V1,
    )
    result = R7RollbackRehearsalGate.evaluate((pre, v2, rolled))
    assert result.verdict == GateVerdict.FAIL
    assert "v2_phase_observed" in result.facts["failed_checks"]


def test_dict_schema_requires_timezone():
    result = R7RollbackRehearsalGate.evaluate_dicts(({
        "phase": "PRE_V1",
        "observed_at": "2026-08-22T10:00:00",
        "capture_engine_version": "V1",
        "capture_v2_production_enabled": False,
        "activation_mode": "V1",
        "v1_healthy": True,
        "v2_producer_count": 0,
        "evidence_kind": "REAL_DUT",
        "evidence_refs": ["evidence://pre"],
    },))
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_OBSERVED_AT_TZ_REQUIRED"


def test_dict_schema_requires_explicit_activation_mode():
    raw = _artifact_from(_valid_real_rehearsal())
    del raw["observations"][1]["activation_mode"]
    result = R7RollbackRehearsalGate.evaluate_artifact(raw)
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_ACTIVATION_MODE_REQUIRED"


def test_dict_schema_rejects_string_booleans_fail_closed():
    raw = _artifact_from(_valid_real_rehearsal())
    raw["observations"][2]["v1_healthy"] = "false"
    result = R7RollbackRehearsalGate.evaluate_artifact(raw)
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_V1_HEALTHY_BOOLEAN_REQUIRED"


def test_artifact_schema_must_match_exactly():
    result = R7RollbackRehearsalGate.evaluate_artifact({
        "schema_version": "future-or-wrong",
        "observations": [],
    })
    assert result.verdict == GateVerdict.INCONCLUSIVE
    assert result.facts["reason"] == "ROLLBACK_EVIDENCE_ARTIFACT_SCHEMA_INVALID"


def test_cli_returns_two_for_incomplete_real_evidence(tmp_path, capsys):
    pre, _, rolled = _valid_real_rehearsal()
    path = tmp_path / "rollback.json"
    import json
    path.write_text(json.dumps(_artifact_from((pre, rolled))), encoding="utf-8")
    assert main(["--evidence", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "DEFERRED_REAL_GATE"

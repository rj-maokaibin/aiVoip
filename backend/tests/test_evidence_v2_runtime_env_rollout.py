from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ENV_PATH = ROOT / "deploy" / "runtime_env.py"
REVISION = "1" * 40

spec = importlib.util.spec_from_file_location("deploy_runtime_env", RUNTIME_ENV_PATH)
assert spec is not None and spec.loader is not None
runtime_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_env)


def _write_base_env(path: Path, project_value: str) -> bytes:
    body = (
        "VOIP_PROJECT_NAME=aivoip\n"
        "PRELIMINARY_EVIDENCE_V2_COMPOSE=true\n"
        "PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true\n"
        f"PRELIMINARY_EVIDENCE_V2_PROJECT={project_value}\n"
        "BUILD_REVISION=0000000000000000000000000000000000000000\n"
    ).encode()
    path.write_bytes(body)
    path.chmod(0o600)
    return body


def _write_rollout(path: Path, *, stage: str, projection: str, strict: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "evidence-v2-production-rollout-v1",
                "stage": stage,
                "canary_selector": "BOUND_REAL_GOLDEN_001",
                "strict_validator": strict,
                "default_projection": projection,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("stage", "projection", "persistent_value", "expected_value"),
    [
        ("SHADOW", "V1", "true", "false"),
        ("CANARY", "V1", "true", "false"),
        ("DEFAULT", "V2", "false", "true"),
    ],
)
def test_rollout_is_authoritative_for_global_projection(
    tmp_path: Path,
    stage: str,
    projection: str,
    persistent_value: str,
    expected_value: str,
) -> None:
    base = tmp_path / "production.env"
    output = tmp_path / "runtime.env"
    rollout = tmp_path / "rollout.json"
    original = _write_base_env(base, persistent_value)
    _write_rollout(rollout, stage=stage, projection=projection)

    removed = runtime_env.materialize(base, output, REVISION, rollout_path=rollout)

    assert removed == 1
    assert base.read_bytes() == original
    rendered = output.read_text(encoding="utf-8")
    assert rendered.count("BUILD_REVISION=") == 1
    assert f"BUILD_REVISION={REVISION}" in rendered
    assert rendered.count("PRELIMINARY_EVIDENCE_V2_PROJECT=") == 1
    assert f"PRELIMINARY_EVIDENCE_V2_PROJECT={expected_value}" in rendered
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("stage", "projection"),
    [
        ("DEFAULT", "V1"),
        ("CANARY", "V2"),
        ("SHADOW", "V2"),
    ],
)
def test_stage_projection_mismatch_fails_closed(tmp_path: Path, stage: str, projection: str) -> None:
    base = tmp_path / "production.env"
    output = tmp_path / "runtime.env"
    rollout = tmp_path / "rollout.json"
    _write_base_env(base, "false")
    _write_rollout(rollout, stage=stage, projection=projection)

    with pytest.raises(RuntimeError, match="projection mismatch"):
        runtime_env.materialize(base, output, REVISION, rollout_path=rollout)

    assert not output.exists()


def test_non_strict_rollout_fails_closed(tmp_path: Path) -> None:
    base = tmp_path / "production.env"
    output = tmp_path / "runtime.env"
    rollout = tmp_path / "rollout.json"
    _write_base_env(base, "false")
    _write_rollout(rollout, stage="DEFAULT", projection="V2", strict=False)

    with pytest.raises(RuntimeError, match="strict_validator=true"):
        runtime_env.materialize(base, output, REVISION, rollout_path=rollout)

    assert not output.exists()


def test_missing_rollout_fails_closed(tmp_path: Path) -> None:
    base = tmp_path / "production.env"
    output = tmp_path / "runtime.env"
    _write_base_env(base, "false")

    with pytest.raises(RuntimeError, match="rollout contract missing"):
        runtime_env.materialize(base, output, REVISION, rollout_path=tmp_path / "missing.json")

    assert not output.exists()

import json
from pathlib import Path

from app.reports.v2.migration import rollout_from_env


ROOT = Path(__file__).resolve().parents[2]


def test_source_controlled_rollout_stage_uses_safe_image_default_and_runtime_overlay():
    policy = json.loads((ROOT / "deploy/evidence_v2_rollout.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    runtime_env = (ROOT / "deploy/runtime_env.py").read_text(encoding="utf-8")
    stage = policy["stage"]

    assert policy["strict_validator"] is True
    assert stage in {"SHADOW", "CANARY", "DEFAULT"}
    assert policy["default_projection"] == ("V2" if stage == "DEFAULT" else "V1")

    # The image stays fail-safe and stage-independent. Production promotion is
    # injected only into the private ephemeral runtime env derived from the
    # exact source-controlled rollout contract; no image rebuild default and no
    # persistent /etc/voip-ai/production.env mutation is authoritative.
    assert "PRELIMINARY_EVIDENCE_V2_COMPOSE=true" in dockerfile
    assert "PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true" in dockerfile
    assert "PRELIMINARY_EVIDENCE_V2_PROJECT=false" in dockerfile
    assert "PRELIMINARY_EVIDENCE_V2_PROJECT_RE" in runtime_env
    assert "evidence_v2_rollout.json" in runtime_env
    assert 'stage == "DEFAULT"' in runtime_env
    assert "persistent_project_entries_ignored" in runtime_env


def test_rollout_switches_do_not_invalidate_expensive_dependency_layers():
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    pip_layer = dockerfile.index("pip install")
    rollout_layer = dockerfile.index("ENV PRELIMINARY_EVIDENCE_V2_COMPOSE=true")
    assert rollout_layer > pip_layer
    assert "DEBIAN_PRIMARY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian" in dockerfile
    assert "PIP_PRIMARY_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
    assert "PIP_FALLBACK_INDEX_URL=https://pypi.org/simple" in dockerfile
    assert "APT_PRIMARY_MIRROR=UNREACHABLE fallback=OFFICIAL" in dockerfile
    assert "PIP_PRIMARY_INDEX=FAIL fallback=OFFICIAL" in dockerfile


def test_domestic_apt_mirror_does_not_replace_official_security_source():
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert 'replace("URIs: http://deb.debian.org/debian\\n", f"URIs: {mirror}\\n")' in dockerfile
    assert 'replace("URIs: https://deb.debian.org/debian\\n", f"URIs: {mirror}\\n")' in dockerfile
    assert 'assert "deb.debian.org/debian-security" in s' in dockerfile
    assert "security=OFFICIAL" in dockerfile


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

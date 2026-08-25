from pathlib import Path

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]


def test_live_acceptance_binds_all_source_roots_to_exact_workspace(monkeypatch):
    monkeypatch.setenv("LIVE_ACCEPTANCE_SOURCE_REVISION", "test-revision")
    monkeypatch.setenv("LIVE_ACCEPTANCE_WORKSPACE_ROOT", str(ROOT))
    settings = Settings(_env_file=None)
    assert settings.profile_root == ROOT / "profiles"
    assert settings.rule_root == ROOT / "rules" / "diagnosis"
    assert settings.knowledge_root == ROOT / "knowledge" / "seed"


def test_normal_settings_keep_production_source_roots(monkeypatch):
    monkeypatch.delenv("LIVE_ACCEPTANCE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("LIVE_ACCEPTANCE_WORKSPACE_ROOT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.profile_root == Path("/app/profiles")
    assert settings.rule_root == Path("/app/rules/diagnosis")
    assert settings.knowledge_root == Path("/app/knowledge/seed")


def test_live_acceptance_source_root_binding_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_ACCEPTANCE_SOURCE_REVISION", "test-revision")
    monkeypatch.setenv("LIVE_ACCEPTANCE_WORKSPACE_ROOT", str(tmp_path))
    try:
        Settings(_env_file=None)
    except ValueError as exc:
        assert "LIVE_ACCEPTANCE_SOURCE_ROOT_INVALID" in str(exc)
    else:
        raise AssertionError("missing exact-head source roots must fail closed")

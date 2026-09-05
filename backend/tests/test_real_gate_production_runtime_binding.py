from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_real_gate_credential_resolvers_follow_formal_production_compose_identity() -> None:
    production_workflow = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    database_resolver = (ROOT / "tools/resolve_real_sip_aba_database_env.py").read_text(encoding="utf-8")
    credential_resolver = (ROOT / "tools/resolve_real_sip_aba_credential_env.py").read_text(encoding="utf-8")
    production_compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert "label=com.docker.compose.project=aivoip" in production_workflow
    assert 'PRODUCTION_PROJECT = "aivoip"' in database_resolver
    assert 'PRODUCTION_PROJECT = "aivoip"' in credential_resolver
    assert 'PRODUCTION_CREDENTIAL_SERVICE = "reproduction-worker"' in database_resolver
    assert 'PRODUCTION_CREDENTIAL_SERVICE = "reproduction-worker"' in credential_resolver
    assert "  reproduction-worker:" in production_compose

    # The old pre-governance compose identity must never be used for production
    # credential discovery; otherwise the real gate sees zero eligible workers.
    assert 'PRODUCTION_PROJECT = "voip-ai"' not in database_resolver
    assert 'com.docker.compose.project\") or \"\") != \"voip-ai\"' not in credential_resolver

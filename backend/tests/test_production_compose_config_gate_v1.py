from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_build_args_are_single_mapping():
    text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    frontend = text.split('\n  frontend:\n', 1)[1]
    build = frontend.split('\n    ports:\n', 1)[0]
    assert build.count('\n      args:\n') == 1
    assert 'BUILD_REVISION:' in build
    assert 'VITE_API_BASE_URL:' in build


def test_source_manifest_gate_runs_production_compose_config_gate():
    workflow = (ROOT / '.github/workflows/source-manifest-gate.yml').read_text(encoding='utf-8')
    assert 'bash tools/production_compose_config_gate.sh' in workflow


def test_compose_config_gate_checks_production_overlay():
    script = (ROOT / 'tools/production_compose_config_gate.sh').read_text(encoding='utf-8')
    assert '-f docker-compose.yml' in script
    assert '-f docker-compose.production.yml' in script
    assert 'config >/dev/null' in script

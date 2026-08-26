from pathlib import Path

from app.core.config import Settings


def test_default_settings_use_root_ssh_username(monkeypatch):
    monkeypatch.delenv('SSH_USERNAME', raising=False)
    cfg = Settings(_env_file=None)
    assert cfg.ssh_username == 'root'


def test_production_template_uses_root_ssh_username():
    text = Path('deploy/production.env.example').read_text(encoding='utf-8')
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert 'SSH_USERNAME=root' in lines
    assert 'SSH_USERNAME=admin' not in lines

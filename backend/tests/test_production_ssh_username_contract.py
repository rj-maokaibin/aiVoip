from pathlib import Path


def test_production_template_uses_root_ssh_username():
    text = Path('deploy/production.env.example').read_text(encoding='utf-8')
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert 'SSH_USERNAME=root' in lines
    assert 'SSH_USERNAME=admin' not in lines

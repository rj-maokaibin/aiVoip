from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.integrations.credentials import CredentialError, LocalSecretCredentialProvider


def _secret_file() -> Path:
    p = Path(tempfile.mkdtemp(prefix='voip-cred-')) / 'secret.yaml'
    p.write_text('''device:
  - name: voip-test-device
    host: 47.104.22.0
    sshport: 64547
    username: root
    password: real-secret-password
''', encoding='utf-8')
    return p


def test_local_secret_provider_resolves_password_by_host():
    provider = LocalSecretCredentialProvider(str(_secret_file()))
    import asyncio
    pw = asyncio.run(provider.get_password(sn='x', ip='47.104.22.0'))
    assert pw == 'real-secret-password'


def test_local_secret_provider_requires_exact_host_match():
    provider = LocalSecretCredentialProvider(str(_secret_file()))
    import asyncio
    with pytest.raises(CredentialError):
        asyncio.run(provider.get_password(sn='x', ip='10.0.0.99'))


def test_local_secret_provider_rejects_missing_file():
    provider = LocalSecretCredentialProvider('/tmp/does-not-exist-secret.yaml')
    import asyncio
    with pytest.raises(CredentialError):
        asyncio.run(provider.get_password(sn='x', ip='47.104.22.0'))


def _mem_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.base import Base
    import app.db.models  # noqa: F401
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_local_secret_provider_prefers_db_credential():
    factory = _mem_factory()
    db = factory()
    from app.db.models import DeviceCredential
    db.add(DeviceCredential(sn='SN-DB', ip='10.0.0.5', ssh_port=2222, username='dbroot',
                            password='db-secret-pw', source='poseidon'))
    db.commit()
    db.close()
    provider = LocalSecretCredentialProvider(str(_secret_file()), session_factory=factory)
    import asyncio
    pw = asyncio.run(provider.get_password(sn='SN-DB', ip='10.0.0.5'))
    assert pw == 'db-secret-pw'
    assert provider.resolve_username(ip='10.0.0.5', fallback='admin') == 'dbroot'


def test_local_secret_provider_falls_back_to_secret_when_no_db_row():
    factory = _mem_factory()  # empty DB
    provider = LocalSecretCredentialProvider(str(_secret_file()), session_factory=factory)
    import asyncio
    pw = asyncio.run(provider.get_password(sn='x', ip='47.104.22.0'))
    assert pw == 'real-secret-password'

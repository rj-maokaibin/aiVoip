import pytest
from app.integrations.credentials import MockCredentialProvider
from app.core.config import settings

@pytest.mark.asyncio
async def test_mock_credential_provider(monkeypatch):
    monkeypatch.setattr(settings, 'mock_device_password', 'secret')
    assert await MockCredentialProvider().get_password(sn='SN1', ip='1.2.3.4') == 'secret'

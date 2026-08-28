from __future__ import annotations

import asyncio

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.context import password_from_source_async
from app.capture_v2.gate.models import GateDeviceSpec


class _Provider:
    provider_id = "poseidon"
    production_capable = True

    def __init__(self):
        self.calls = []

    async def get_password(self, *, sn: str, ip: str, product: str | None = None) -> str:
        self.calls.append((sn, ip, product))
        return "runtime-only-secret"


class _NonProductionProvider(_Provider):
    provider_id = "mock"
    production_capable = False


def _spec() -> GateDeviceSpec:
    return GateDeviceSpec(
        device_id="device-1",
        model="APF3260-M",
        host="10.48.8.74",
        port=10002,
        username="root",
        platform_id="mt7981",
    )


def test_provider_source_uses_production_provider_without_db_password(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(
        "app.integrations.credentials.get_credential_provider",
        lambda: provider,
    )

    password = asyncio.run(password_from_source_async("PROVIDER:SN-001", _spec()))

    assert password == "runtime-only-secret"
    assert provider.calls == [("SN-001", "10.48.8.74", "APF3260-M")]


def test_provider_source_fails_closed_for_non_production_provider(monkeypatch):
    provider = _NonProductionProvider()
    monkeypatch.setattr(
        "app.integrations.credentials.get_credential_provider",
        lambda: provider,
    )

    with pytest.raises(CaptureV2Error) as exc:
        asyncio.run(password_from_source_async("PROVIDER:SN-001", _spec()))

    assert exc.value.code == "CAPTURE_GATE_CREDENTIAL_PROVIDER_NOT_PRODUCTION_CAPABLE"
    assert provider.calls == []


def test_env_source_remains_backward_compatible(monkeypatch):
    monkeypatch.setenv("GATE_TEST_PASSWORD", "env-secret")

    password = asyncio.run(password_from_source_async("ENV:GATE_TEST_PASSWORD", _spec()))

    assert password == "env-secret"

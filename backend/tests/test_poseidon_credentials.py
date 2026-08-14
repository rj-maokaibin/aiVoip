from __future__ import annotations

import pytest

from app.integrations.credentials import CredentialError, PoseidonCredentialProvider
from app.integrations.poseidon import PoseidonClient


class _FakeResponse:
    def __init__(self, json_data=None, status=200, headers=None, text=""):
        self._json = json_data or {}
        self.status_code = status
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Fake httpx.AsyncClient returning canned devKey responses."""

    def __init__(self, *, page_items=None):
        self._page_items = page_items if page_items is not None else []
        self.closed = False

    async def post(self, url, **kwargs):
        if "/devKey/add" in url:
            return _FakeResponse(json_data={"code": 0, "msg": "ok."})
        if "/devKey/page" in url:
            return _FakeResponse(json_data={"code": 0, "dataList": self._page_items})
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url, **kwargs):
        return _FakeResponse(json_data={}, status=200)

    async def aclose(self):
        self.closed = True


def test_poseidon_provider_returns_sshpassv1_priority():
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-1", "sshpassv1": "v1pw", "sshpassv2": "v2pw"},
    ])
    client = PoseidonClient(client=fake)
    provider = PoseidonCredentialProvider(client=client)
    import asyncio
    pw = asyncio.run(provider.get_password(sn="SN-1", ip="10.0.0.1"))
    # v1 is preferred (older firmware such as APF3260-M uses the v1 mechanism).
    assert pw == "v1pw"


def test_poseidon_provider_falls_back_to_v2_when_v1_missing():
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-1", "sshpassv1": "", "sshpassv2": "v2pw"},
    ])
    client = PoseidonClient(client=fake)
    provider = PoseidonCredentialProvider(client=client)
    import asyncio
    pw = asyncio.run(provider.get_password(sn="SN-1", ip="10.0.0.1"))
    assert pw == "v2pw"


def test_poseidon_provider_raises_when_no_record():
    fake = _FakeAsyncClient(page_items=[])
    client = PoseidonClient(client=fake)
    provider = PoseidonCredentialProvider(client=client)
    import asyncio
    with pytest.raises(CredentialError):
        asyncio.run(provider.get_password(sn="SN-1", ip="10.0.0.1"))


def test_poseidon_provider_requires_sn():
    provider = PoseidonCredentialProvider()
    import asyncio
    with pytest.raises(CredentialError):
        asyncio.run(provider.get_password(sn="", ip="10.0.0.1"))


def test_poseidon_provider_username_fallback_root():
    provider = PoseidonCredentialProvider()
    assert provider.resolve_username(ip="10.0.0.1", fallback="admin") == "admin"
    assert provider.resolve_username(ip="10.0.0.1", fallback=None) == "root"


def test_poseidon_client_get_ssh_pass_parses_v1_v2():
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-2", "sshpassv1": "a1", "sshpassv2": "a2"},
    ])
    client = PoseidonClient(client=fake)
    import asyncio
    v1, v2 = asyncio.run(client.get_ssh_pass(sn="SN-2"))
    assert (v1, v2) == ("a1", "a2")
    assert fake.closed is True


def test_poseidon_client_get_device_record_returns_mac_and_product():
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-3", "mac": "98:4A:6B:D0:25:01", "productClass": "APF3260-M",
         "sshpassv1": "v1pw", "sshpassv2": "v2pw"},
    ])
    client = PoseidonClient(client=fake)
    import asyncio
    rec = asyncio.run(client.get_device_record(sn="SN-3"))
    assert rec["sn"] == "SN-3"
    assert rec["mac"] == "98:4A:6B:D0:25:01"
    assert rec["product_class"] == "APF3260-M"
    assert rec["sshpassv1"] == "v1pw"
    assert rec["sshpassv2"] == "v2pw"
    assert fake.closed is True


def test_poseidon_client_get_device_record_empty_fields():
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-4", "mac": "", "productClass": "", "sshpassv1": "p1", "sshpassv2": ""},
    ])
    client = PoseidonClient(client=fake)
    import asyncio
    rec = asyncio.run(client.get_device_record(sn="SN-4"))
    assert rec["mac"] is None
    assert rec["product_class"] is None
    assert rec["sshpassv1"] == "p1"
    assert rec["sshpassv2"] == ""


def test_poseidon_client_get_device_record_prefers_password_row():
    """Poseidon returns several rows for one SN (failed attempts then the success
    row). The password-bearing row must be chosen so SSH creds stay resolvable."""
    fake = _FakeAsyncClient(page_items=[
        {"sn": "SN-5", "mac": "98:4A:6B:D0:25:01", "productClass": "APF3260-M",
         "sshpassv1": "", "sshpassv2": "", "status": "error"},
        {"sn": "SN-5", "mac": "98:4A:6B:D0:25:01", "productClass": "APF3260-M",
         "sshpassv1": "117915c7", "sshpassv2": "%L*^^2b2", "status": "success"},
    ])
    client = PoseidonClient(client=fake)
    import asyncio
    rec = asyncio.run(client.get_device_record(sn="SN-5"))
    assert rec["sshpassv1"] == "117915c7"
    assert rec["sshpassv2"] == "%L*^^2b2"
    assert rec["mac"] == "98:4A:6B:D0:25:01"
    assert rec["product_class"] == "APF3260-M"


def test_poseidon_client_get_device_record_raises_when_no_row():
    fake = _FakeAsyncClient(page_items=[])
    client = PoseidonClient(client=fake)
    import asyncio
    with pytest.raises(CredentialError):
        asyncio.run(client.get_device_record(sn="SN-6"))

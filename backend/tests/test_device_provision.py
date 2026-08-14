from __future__ import annotations

import asyncio

import pytest

from app.integrations.credentials import CredentialError
from app.integrations.feishu.device_provision import DeviceProvisioner
from app.integrations.feishu.device_request import parse_device_request
from app.integrations.poseidon import PoseidonClient
from app.integrations.ssh_opener import LuciSshOpener, SshOpenerError


class _FakePoseidon:
    async def get_ssh_pass(self, *, sn, mac=None, product=None):
        return "v1pw", "v2pw"


class _FakeOpener:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def set_ssh(self, *, web_url=None, host=None, scheme="http", mode=1, sid=None):
        self.calls.append((web_url, host, mode))
        if self.fail:
            raise SshOpenerError("SSH_OPENER_FAILED:test")
        return {"code": 0}


# ---- parser ----

def test_parse_full_message():
    r = parse_device_request(
        "打开SSH sn=G1S0A0F057620 web=https://abc.noc.rj.link/cgi-bin/luci/?stamp=1 "
        "ip=10.44.77.254 port=2222 mac=F0:74:8D:E1:9C:E2 product=APF1250"
    )
    assert r.sn == "G1S0A0F057620"
    assert r.ssh_ip == "10.44.77.254"
    assert r.ssh_port == 2222
    assert r.web_url.startswith("https://abc.noc.rj.link")
    assert r.mac == "F0:74:8D:E1:9C:E2"
    assert r.product == "APF1250"
    assert r.is_open_intent() is True
    assert r.has_minimal() is True


def test_parse_minimal_sn_and_ip():
    r = parse_device_request("开ssh sn=ABC123 ip=10.1.2.3")
    assert r.sn == "ABC123"
    assert r.ssh_ip == "10.1.2.3"
    assert r.ssh_port == 22  # default
    assert r.has_minimal() is True


def test_parse_web_url_only():
    r = parse_device_request("打开SSH https://x.noc.rj.link/cgi-bin/luci/?stamp=9 sn=ZZZ")
    assert r.web_url.startswith("https://x.noc.rj.link")
    assert r.sn == "ZZZ"


def test_parse_empty_raises():
    with pytest.raises(CredentialError):
        parse_device_request("")


def test_parse_invalid_port_raises():
    with pytest.raises(CredentialError):
        parse_device_request("打开ssh sn=1 ip=10.0.0.1 port=abc")


# ---- provisioner (DB storage) ----


def _mem_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.base import Base
    import app.db.models  # noqa: F401  (register all tables on Base.metadata)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_provision_opens_ssh_and_stores_in_db():
    factory = _mem_factory()
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), session_factory=factory)
    res = asyncio.run(prov.provision(
        web_url="https://x.noc.rj.link/cgi-bin/luci/?stamp=1",
        ssh_ip="10.44.77.254", ssh_port=2222, sn="SN-1", mac="M", product="P",
    ))
    assert res["ssh_opened"] is True
    assert res["password_resolved"] is True
    assert res["stored_in_db"] is True
    db = factory()
    try:
        from app.db.models import DeviceCredential
        from sqlalchemy import select
        row = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == "SN-1"))
        assert row is not None
        assert row.ip == "10.44.77.254"
        assert row.ssh_port == 2222
        assert row.username == "root"
        assert row.password == "v1pw"
        assert row.mac == "M"
        assert row.product == "P"
    finally:
        db.close()


def test_provision_upserts_existing_sn_in_db():
    factory = _mem_factory()
    db = factory()
    from app.db.models import DeviceCredential
    db.add(DeviceCredential(sn="SN-1", ip="10.44.77.254", ssh_port=2222, username="root",
                            password="old", source="poseidon"))
    db.commit()
    db.close()
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), session_factory=factory)
    asyncio.run(prov.provision(web_url=None, ssh_ip="10.44.77.254", ssh_port=2222, sn="SN-1"))
    db = factory()
    try:
        from sqlalchemy import select
        row = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == "SN-1"))
        assert row.password == "v1pw"
    finally:
        db.close()


def test_provision_extracts_sn_from_web_url():
    factory = _mem_factory()
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), session_factory=factory)
    # sn not passed; extract_sn_from_web_url would hit the network, so monkeypatch it.
    import app.integrations.feishu.device_provision as dp
    async def fake_extract(url):
        return "SN-FROM-URL"
    orig = dp.extract_sn_from_web_url
    dp.extract_sn_from_web_url = fake_extract
    try:
        res = asyncio.run(prov.provision(web_url="https://x.noc.rj.link/", ssh_ip=None,
                                         ssh_port=22, sn=None))
        assert res["sn"] == "SN-FROM-URL"
    finally:
        dp.extract_sn_from_web_url = orig


def test_provision_missing_sn_raises():
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), session_factory=_mem_factory())
    with pytest.raises(CredentialError):
        asyncio.run(prov.provision(web_url=None, ssh_ip="10.0.0.1", ssh_port=22, sn=""))


def test_provision_open_failure_raises():
    prov = DeviceProvisioner(opener=_FakeOpener(fail=True), poseidon=_FakePoseidon(), session_factory=_mem_factory())
    with pytest.raises(CredentialError):
        asyncio.run(prov.provision(web_url="https://x/", ssh_ip="10.0.0.1", ssh_port=22, sn="SN-1"))

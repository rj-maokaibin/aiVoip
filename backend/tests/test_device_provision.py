from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
import yaml

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


# ---- provisioner ----

def test_provision_opens_ssh_and_upserts_secret():
    secret = Path(tempfile.mkdtemp(prefix="voip-prov-")) / "secret.yaml"
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), secret_file=str(secret))
    res = asyncio.run(prov.provision(
        web_url="https://x.noc.rj.link/cgi-bin/luci/?stamp=1",
        ssh_ip="10.44.77.254", ssh_port=2222, sn="SN-1", mac="M", product="P",
    ))
    assert res["ssh_opened"] is True
    assert res["password_resolved"] is True
    # secret.yaml updated for the reproduction provider.
    data = yaml.safe_load(secret.read_text(encoding="utf-8"))
    dev = data["device"][0]
    assert dev["host"] == "10.44.77.254"
    assert dev["sshport"] == "2222"
    assert dev["username"] == "root"
    assert dev["password"] == "v2pw"


def test_provision_matches_existing_secret_entry_and_updates_password():
    secret = Path(tempfile.mkdtemp(prefix="voip-prov-")) / "secret.yaml"
    secret.write_text(yaml.safe_dump({
        "device": [{"name": "SN-1", "host": "10.44.77.254", "sshport": "2222",
                    "username": "root", "password": "old"}]
    }, allow_unicode=True), encoding="utf-8")
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(), secret_file=str(secret))
    asyncio.run(prov.provision(
        web_url="https://x.noc.rj.link/", ssh_ip="10.44.77.254", ssh_port=2222, sn="SN-1",
    ))
    data = yaml.safe_load(secret.read_text(encoding="utf-8"))
    assert len(data["device"]) == 1
    assert data["device"][0]["password"] == "v2pw"


def test_provision_missing_sn_raises():
    prov = DeviceProvisioner(opener=_FakeOpener(), poseidon=_FakePoseidon(),
                             secret_file="/tmp/nonexistent-secret.yaml")
    with pytest.raises(CredentialError):
        asyncio.run(prov.provision(web_url=None, ssh_ip="10.0.0.1", ssh_port=22, sn=""))


def test_provision_open_failure_raises():
    prov = DeviceProvisioner(opener=_FakeOpener(fail=True), poseidon=_FakePoseidon(),
                             secret_file="/tmp/nonexistent-secret.yaml")
    with pytest.raises(CredentialError):
        asyncio.run(prov.provision(web_url="https://x/", ssh_ip="10.0.0.1", ssh_port=22, sn="SN-1"))

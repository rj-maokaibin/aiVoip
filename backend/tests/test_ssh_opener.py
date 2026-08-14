from __future__ import annotations

import asyncio

import pytest

from app.integrations.ssh_opener import LuciSshOpener, SshOpenerError

_PAGE = "<html><script>var sid = '9f60f4dd89cda8291234567890abcdef';</script></html>"


class _FakeResponse:
    def __init__(self, text="", json_data=None, status=200):
        self.text = text
        self._json = json_data or {}
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self):
        self.gets = []
        self.posts = []

    async def get(self, url, **kwargs):
        self.gets.append(url)
        return _FakeResponse(text=_PAGE)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        return _FakeResponse(json_data={"code": 0, "data": {"result": "success"}})

    async def aclose(self):
        pass


def test_opener_opens_ssh_via_web_url():
    fake = _FakeClient()
    opener = LuciSshOpener(client=fake)
    result = asyncio.run(opener.set_ssh(
        web_url="https://f6685747hd.noc.rj.link/cgi-bin/luci/;stok=abc/master?stamp=1&pass=x",
        mode=1,
    ))
    assert result["code"] == 0
    # GET the page to extract sid, then POST devSta.set developMode=1.
    assert len(fake.gets) == 1
    url, body = fake.posts[0]
    assert "auth=9f60f4dd89cda8291234567890abcdef" in url
    assert body["method"] == "devSta.set"
    assert body["params"]["data"]["developMode"] == "1"
    assert url.startswith("https://f6685747hd.noc.rj.link")


def test_opener_closes_ssh():
    fake = _FakeClient()
    opener = LuciSshOpener(client=fake)
    result = asyncio.run(opener.set_ssh(
        web_url="https://host.noc.rj.link/cgi-bin/luci/;stok=abc/master?stamp=1",
        mode=0,
    ))
    assert result["code"] == 0
    _, body = fake.posts[0]
    assert body["params"]["data"]["developMode"] == "0"


def test_opener_requires_web_url_or_sid():
    opener = LuciSshOpener(client=_FakeClient())
    with pytest.raises(SshOpenerError):
        asyncio.run(opener.set_ssh(web_url=None, sid=None, host=None, mode=1))


def test_opener_sid_not_found():
    class _NoSid(_FakeClient):
        async def get(self, url, **kwargs):
            return _FakeResponse(text="<html>no sid here</html>")

    opener = LuciSshOpener(client=_NoSid())
    with pytest.raises(SshOpenerError):
        asyncio.run(opener.set_ssh(web_url="https://x.noc.rj.link/", mode=1))

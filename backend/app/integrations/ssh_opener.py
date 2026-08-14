"""Open the SSH service on a ReyeeOS DUT via its Web (LuCI) API.

Ports macc_open_ssh.open_device_ssh_port to httpx. A field engineer provides a Web
tunnel URL (EWEB) or direct device host; this module:
  1. GETs the Web page and extracts the `sid` from its JS;
  2. POSTs /cgi-bin/luci/api/cmd?auth=<sid> devSta.set develop_mode developMode=1
     to turn on the SSH service (SSH port opens).

The resulting SSH password is resolved separately via Poseidon (see poseidon.py).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from app.integrations.credentials import CredentialError

API_TIMEOUT = 20.0

_SID_RE = re.compile(r"var sid = '([a-f0-9]{32})'")


class SshOpenerError(RuntimeError):
    pass


class LuciSshOpener:
    """Turn on/off the DUT SSH service through the LuCI Web API."""

    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._client = client

    async def _session(self) -> httpx.AsyncClient:
        return self._client if self._client is not None else httpx.AsyncClient(
            timeout=API_TIMEOUT, verify=False
        )

    async def set_ssh(self, *, web_url: str | None = None, host: str | None = None,
                      scheme: str = "http", mode: int = 1, sid: str | None = None) -> dict:
        """Open (mode=1) or close (mode=0) the DUT SSH service.

        web_url: EWEB tunnel URL (session cookie + sid parsed from its page), or
        host: direct device host with optional pre-fetched sid.
        """
        s = await self._session()
        try:
            if sid is None:
                if not web_url:
                    raise SshOpenerError("SSH_OPENER_WEB_URL_OR_SID_REQUIRED")
                page = (await s.get(web_url, verify=False)).text
                m = _SID_RE.search(page)
                if not m:
                    raise SshOpenerError("SSH_OPENER_SID_NOT_FOUND")
                sid = m.group(1)
                host = urlparse(web_url).hostname
                scheme = urlparse(web_url).scheme
            if not host:
                raise SshOpenerError("SSH_OPENER_HOST_REQUIRED")
            url = f"{scheme}://{host}/cgi-bin/luci/api/cmd?auth={sid}"
            resp = await s.post(
                url,
                json={
                    "method": "devSta.set",
                    "params": {"module": "develop_mode",
                               "data": {"developMode": str(mode)}, "device": "pc"},
                },
                headers={"Content-Type": "application/json"},
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()
        except CredentialError:
            raise
        except SshOpenerError:
            raise
        except Exception as exc:
            raise SshOpenerError(f"SSH_OPENER_FAILED:{type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await s.aclose()

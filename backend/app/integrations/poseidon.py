"""Poseidon SSH-password credential provider (httpx port of macc_open_ssh.py).

技服通过飞书提供 SN/IP 后，系统经百川统一认证(baichuan)登录波塞冬(ops.rj.link),
调用 devKey/add + devKey/page 生成/读取设备 SSH 密码（sshpassv1/v2），
作为 aiVoip 后台复现平台的 DUT 凭据供给 —— 不回传给技服，只供本系统后台使用。

与 macc_open_ssh.py 的区别：
- 用 httpx（项目统一 HTTP 客户端）替代 requests；
- 密码不回传、不落日志；只返回给调用方（后台复现凭据解析）；
- 凭据从 ~/secret.yaml 的 sso.baichuan 读取（与 ai-utils 统一凭据一致）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from app.core.config import settings
from app.integrations.credentials import CredentialError

BAICHUAN = "https://baichuan.rj.link/datacenter"
POSEIDON = "https://ops.rj.link/poseidon/api"
API_TIMEOUT = 20.0


def _secret_file() -> Path:
    return Path(os.environ.get("LOCAL_SECRET_FILE", "/home/dev/secret.yaml"))


def _load_secrets() -> dict:
    path = _secret_file()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _baichuan_credentials() -> tuple[str, str]:
    cfg = _load_secrets()
    bc = (cfg.get("sso") or {}).get("baichuan") or {}
    user, pwd = bc.get("username") or "", bc.get("password") or ""
    if not user or not pwd:
        raise CredentialError("POSEIDON_BAICHUAN_CREDENTIALS_MISSING")
    return user, pwd


class PoseidonClient:
    """波塞冬 SSH 密码查询客户端（httpx 实现）。"""

    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._client = client

    async def _session(self) -> httpx.AsyncClient:
        """登录百川认证中心，返回带 ops.rj.link 会话的 AsyncClient。"""
        if self._client is not None:
            return self._client
        user, pwd = _baichuan_credentials()
        s = httpx.AsyncClient(timeout=API_TIMEOUT, follow_redirects=False)
        try:
            d1 = (await s.post(f"{BAICHUAN}/twofa/password", data={"name": user, "pwd": pwd})).json()
            ak = (d1.get("data") or {}).get("accessKey")
            if not ak:
                raise CredentialError("POSEIDON_TWOFA_LOGIN_FAILED")
            await s.post(f"{BAICHUAN}/sso/doLogin", data={"name": user, "pwd": ak})
            auth_url = (
                f"{BAICHUAN}/sso/auth?redirect="
                + quote("http://ops.rj.link/poseidon/api/sso/login?back=https://ops.rj.link/poseidon/")
            )
            # Follow the full redirect chain (baichuan sso/auth -> ops.rj.link sso/login
            # -> http->https 301 -> final) so ops.rj.link session cookies are set. Manual
            # following is used because follow_redirects=False keeps the ticket cookie.
            r = await s.get(auth_url)
            seen = 0
            while r.headers.get("Location") and seen < 8:
                r = await s.get(r.headers["Location"])
                seen += 1
            return s
        except CredentialError:
            await s.aclose()
            raise
        except Exception as exc:
            await s.aclose()
            raise CredentialError(f"POSEIDON_SESSION_FAILED:{type(exc).__name__}") from exc

    async def get_ssh_pass(self, *, sn: str, mac: str | None = None, product: str | None = None) -> tuple[str, str]:
        """生成并读取设备 SSH 密码，返回 (sshpassv1, sshpassv2)。密码不回传飞书、不落日志。"""
        s = await self._session()
        try:
            add = await s.post(f"{POSEIDON}/devKey/add?cloud=cn", json=[{"sn": sn, "mac": mac, "productClass": product}])
            add.raise_for_status()
            # Poseidon generates the key asynchronously; wait a moment before reading it
            # back (mirrors macc_open_ssh.py's time.sleep(2)).
            await asyncio.sleep(2)
            page = await s.post(
                f"{POSEIDON}/devKey/page?cloud=cn",
                json={"page": 1, "pageSize": 10, "sn": sn},
            )
            page.raise_for_status()
            data = page.json()
            for item in data.get("dataList") or []:
                if item.get("sn") == sn:
                    v1 = item.get("sshpassv1")
                    v2 = item.get("sshpassv2")
                    if v1 or v2:
                        return str(v1 or ""), str(v2 or "")
            raise CredentialError("POSEIDON_DEVKEY_NOT_FOUND")
        finally:
            await s.aclose()

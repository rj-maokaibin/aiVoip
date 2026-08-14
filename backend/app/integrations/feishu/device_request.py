"""Parse a Feishu text message into a device-access request.

Field engineers send (in a group @bot or DM) something like:

    打开SSH sn=G1S0A0F057620 web=https://<sn>.noc.rj.link/cgi-bin/luci/?stamp=..
    ip=10.44.77.254 port=22 mac=F0:74:8D:E1:9C:E2 product=APF1250

The parser extracts web_url / ssh_ip / ssh_port / sn / mac / product and validates
the minimal set needed to open SSH and resolve the Poseidon password.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.integrations.credentials import CredentialError

# HTTP(S) URL, or a bare IPv4 host.
_URL_RE = re.compile(r"https?://[^\s，。]+", re.IGNORECASE)
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_KV_RE = re.compile(r"(?i)\b(sn|mac|product|ip|port|web|url)\s*[=:]\s*(\S+)")


@dataclass(frozen=True)
class DeviceAccessRequest:
    web_url: str | None = None
    ssh_ip: str | None = None
    ssh_port: int = 22
    sn: str | None = None
    mac: str | None = None
    product: str | None = None
    raw: str = ""

    def is_open_intent(self) -> bool:
        t = self.raw.lower()
        return any(k in t for k in ("打开ssh", "开ssh", "open ssh", "openssh", "打开 ssh", "开启ssh"))

    def has_minimal(self) -> bool:
        """At least a web_url or (ssh_ip and sn) is needed to proceed."""
        return bool(self.web_url) or bool(self.ssh_ip and self.sn)


def parse_device_request(text: str) -> DeviceAccessRequest:
    if not text or not text.strip():
        raise CredentialError("DEVICE_REQUEST_EMPTY")
    kv = {}
    for k, v in _KV_RE.findall(text):
        kv[k.lower()] = v.strip().strip("'\"")
    url = _URL_RE.search(text)
    web_url = kv.get("web") or kv.get("url") or (url.group(0) if url else None)
    ip = kv.get("ip")
    if not ip:
        m = _IP_RE.search(text)
        if m:
            ip = m.group(1)
    port = 22
    if kv.get("port"):
        try:
            port = int(kv["port"])
        except ValueError:
            raise CredentialError("DEVICE_REQUEST_INVALID_PORT")
    return DeviceAccessRequest(
        web_url=web_url,
        ssh_ip=ip,
        ssh_port=port,
        sn=kv.get("sn"),
        mac=kv.get("mac"),
        product=kv.get("product"),
        raw=text,
    )

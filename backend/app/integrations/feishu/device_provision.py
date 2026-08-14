"""Provision a DUT for background reproduction from a Feishu device request.

Pipeline:
  1. parse the Feishu message -> DeviceAccessRequest
  2. extract the SN from the EWEB page JS (var sn = '...') when not provided
  3. open the SSH service on the DUT (via Web/LuCI API) if a web_url was given
  4. resolve the SSH password from Poseidon (by SN); Poseidon also yields MAC/product
  5. upsert host/port/user/password into the device_credentials table (DB), NOT
     secret.yaml, so the reproduction platform reads credentials from the DB.

The password is never returned to the engineer and never logged.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from sqlalchemy import select
import httpx

from app.integrations.credentials import CredentialError
from app.integrations.poseidon import PoseidonClient
from app.integrations.ssh_opener import LuciSshOpener, SshOpenerError

_SN_RE = re.compile(r"var sn = '([^']+)'")


async def extract_sn_from_web_url(web_url: str) -> str | None:
    """Read the EWEB LuCI page and return its `var sn = '...'` value if present."""
    async with httpx.AsyncClient(timeout=20, verify=False) as s:
        page = (await s.get(web_url)).text
    m = _SN_RE.search(page)
    return m.group(1) if m else None


class DeviceProvisioner:
    def __init__(self, *, opener: LuciSshOpener | None = None,
                 poseidon: PoseidonClient | None = None,
                 session_factory=None):
        self._opener = opener or LuciSshOpener()
        self._poseidon = poseidon or PoseidonClient()
        self._session_factory = session_factory

    def _db(self):
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal
        return SessionLocal()

    async def provision(self, *, web_url: str | None, ssh_ip: str | None,
                        ssh_port: int, sn: str | None = None, mac: str | None = None,
                        product: str | None = None) -> dict:
        """Open SSH, resolve the Poseidon password, and upsert into device_credentials.

        Returns a status dict (no password included).
        """
        # 1. Resolve SN: explicit > web url page.
        if not sn and web_url:
            sn = await extract_sn_from_web_url(web_url)
        if not sn:
            raise CredentialError("DEVICE_PROVISION_SN_REQUIRED")

        # 2. Open the SSH service when a web_url is available.
        ssh_opened = False
        if web_url:
            try:
                await self._opener.set_ssh(web_url=web_url, mode=1)
                ssh_opened = True
            except SshOpenerError as exc:
                raise CredentialError(f"DEVICE_SSH_OPEN_FAILED:{exc}") from exc

        # 3. Resolve the SSH password from Poseidon (also yields MAC/product).
        record = await self._poseidon.get_device_record(sn=sn, mac=mac, product=product)
        v1, v2 = record["sshpassv1"], record["sshpassv2"]
        # Older firmware (e.g. APF1250 2.387, APF3260-M) uses the v1 mechanism; v2 is
        # rejected. Prefer v1, fall back to v2 (matches README_macc_open_ssh).
        password = v1 or v2
        if not password:
            # Poseidon returned no password (expired/rotated devKey record). Do not
            # fail the whole provision if we already hold a working password for this
            # SN in device_credentials: keep it and only refresh address/model.
            existing = self._existing_password(sn=sn)
            if existing:
                password = existing
            else:
                raise CredentialError("DEVICE_POSEIDON_PASSWORD_MISSING")

        # Backfill MAC/model from the Poseidon record when the engineer did not
        # provide them: the EWEB (MACC relay) page does not expose device identity,
        # but Poseidon's devKey record carries mac + productClass for known SNs.
        mac = mac or record.get("mac")
        product = product or record.get("product_class")

        # 4. Upsert into device_credentials (DB), not secret.yaml.
        # In web_url (EWEB tunnel) mode there is no direct SSH IP; fall back to the
        # tunnel hostname so the row is addressable and can be updated later with the
        # real SSH tunnel endpoint.
        if not ssh_ip and web_url:
            ssh_ip = urlparse(web_url).hostname
        self._upsert_db(ssh_ip=ssh_ip, ssh_port=ssh_port, sn=sn, password=password,
                        mac=mac, product=product, web_url=web_url)

        return {
            "sn": sn,
            "ssh_ip": ssh_ip,
            "ssh_port": ssh_port,
            "ssh_opened": ssh_opened,
            "password_resolved": True,
            "stored_in_db": True,
        }

    def _upsert_db(self, *, ssh_ip: str, ssh_port: int, sn: str, password: str,
                   mac: str | None, product: str | None, web_url: str | None) -> None:
        from app.db.models import DeviceCredential
        db = self._db()
        try:
            row = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == sn))
            if row is None:
                row = DeviceCredential(sn=sn)
                db.add(row)
            if ssh_ip:
                row.ip = ssh_ip
            if ssh_port:
                row.ssh_port = ssh_port
            row.username = "root"
            row.password = password
            if mac:
                row.mac = mac
            if product:
                row.product = product
            if web_url:
                row.web_url = web_url
            row.source = "poseidon"
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _existing_password(self, *, sn: str) -> str:
        """Return the current device_credentials password for sn, or '' if none."""
        from app.db.models import DeviceCredential
        db = self._db()
        try:
            row = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == sn))
            return str(row.password or "") if row else ""
        finally:
            db.close()

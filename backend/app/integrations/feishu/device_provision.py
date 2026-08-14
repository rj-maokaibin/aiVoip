"""Provision a DUT for background reproduction from a Feishu device request.

Pipeline:
  1. parse the Feishu message -> DeviceAccessRequest
  2. open the SSH service on the DUT (via Web/LuCI API) if a web_url was given
  3. resolve the SSH password from Poseidon (by SN)
  4. upsert the DUT into ~/secret.yaml so the local_secret credential provider
     (used by the reproduction platform) picks up host/port/user/password

The password is never returned to the engineer and never logged.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml

from app.integrations.credentials import CredentialError
from app.integrations.poseidon import PoseidonClient
from app.integrations.ssh_opener import LuciSshOpener, SshOpenerError

_LOCK = threading.Lock()


def _secret_file() -> Path:
    return Path(os.environ.get("LOCAL_SECRET_FILE", "/home/dev/secret.yaml"))


class DeviceProvisioner:
    def __init__(self, *, opener: LuciSshOpener | None = None,
                 poseidon: PoseidonClient | None = None,
                 secret_file: str | None = None):
        self._opener = opener or LuciSshOpener()
        self._poseidon = poseidon or PoseidonClient()
        self._secret_file = Path(secret_file) if secret_file else _secret_file()

    async def provision(self, *, web_url: str | None, ssh_ip: str | None,
                        ssh_port: int, sn: str, mac: str | None = None,
                        product: str | None = None) -> dict:
        """Open SSH, resolve the Poseidon password, and upsert into secret.yaml.

        Returns a status dict (no password included).
        """
        if not sn:
            raise CredentialError("DEVICE_PROVISION_SN_REQUIRED")

        # 1. Open the SSH service when a web_url is available.
        ssh_opened = False
        if web_url:
            try:
                await self._opener.set_ssh(web_url=web_url, mode=1)
                ssh_opened = True
            except SshOpenerError as exc:
                # Opening the port is best-effort; a password may still be usable if the
                # device SSH is already open. Surface but do not fail the whole provision.
                raise CredentialError(f"DEVICE_SSH_OPEN_FAILED:{exc}") from exc

        # 2. Resolve the SSH password from Poseidon.
        v1, v2 = await self._poseidon.get_ssh_pass(sn=sn, mac=mac, product=product)
        password = v2 or v1
        if not password:
            raise CredentialError("DEVICE_POSEIDON_PASSWORD_MISSING")

        # 3. Upsert into secret.yaml (host/port/user/password) for the reproduction
        #    platform's local_secret credential provider.
        self._upsert_secret(ssh_ip=ssh_ip, ssh_port=ssh_port, sn=sn, password=password)

        return {
            "sn": sn,
            "ssh_ip": ssh_ip,
            "ssh_port": ssh_port,
            "ssh_opened": ssh_opened,
            "password_resolved": True,
            "secret_updated": True,
        }

    def _upsert_secret(self, *, ssh_ip: str, ssh_port: int, sn: str, password: str) -> None:
        path = self._secret_file
        with _LOCK:
            data = {}
            if path.exists():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            devices = data.get("device")
            if not isinstance(devices, list):
                devices = []
            # Match by host+port, else by sn; update in place or append.
            updated = False
            for dev in devices:
                if isinstance(dev, dict) and (
                    (dev.get("host") == ssh_ip and str(dev.get("sshport", "")) == str(ssh_port))
                    or dev.get("name") == sn
                ):
                    dev["host"] = ssh_ip
                    dev["sshport"] = str(ssh_port)
                    dev["username"] = "root"
                    dev["password"] = password
                    updated = True
                    break
            if not updated:
                devices.append({
                    "name": sn, "host": ssh_ip, "sshport": str(ssh_port),
                    "username": "root", "password": password,
                })
            data["device"] = devices
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.automation.adapters.pbx.profile import FusionPbxLabProfile


class FusionPbxConfigProviderError(RuntimeError):
    pass


PhpRunner = Callable[[str, dict[str, Any], float], tuple[int | None, str]]
_EXTENSION_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,64}$")


@dataclass(frozen=True)
class FusionPbxDomain:
    domain_id: str
    domain_name: str


@dataclass(frozen=True)
class FusionPbxExtensionView:
    domain_id: str
    extension_uuid: str
    extension: str
    number_alias: str | None
    enabled: bool
    description: str | None
    accountcode: str | None
    user_context: str | None
    password_present: bool


_READ_PROVIDER_PHP = r'''<?php
declare(strict_types=1);
$req = json_decode(stream_get_contents(STDIN), true);
if (!is_array($req)) { fwrite(STDERR, "PBX_REQUEST_INVALID\n"); exit(2); }
$root = strval($req['fusionpbx_root'] ?? '');
if ($root === '' || $root[0] !== '/') { fwrite(STDERR, "PBX_ROOT_INVALID\n"); exit(3); }
require_once $root . '/resources/require.php';
global $database;
if (!$database) { fwrite(STDERR, "PBX_DATABASE_UNAVAILABLE\n"); exit(4); }
$action = strval($req['action'] ?? '');
$out = ['mutation_executed'=>false, 'secret_values_emitted'=>false, 'action'=>$action];
if ($action === 'domains') {
  $rows = $database->select("select domain_uuid, domain_name from v_domains where domain_enabled = true order by domain_name", null, 'all');
  $out['rows'] = is_array($rows) ? $rows : [];
}
elseif ($action === 'extension') {
  $identity = strval($req['identity'] ?? '');
  $rows = $database->select(
    "select domain_uuid, extension_uuid, extension, number_alias, enabled, description, accountcode, user_context, case when password is not null and password <> '' then true else false end as password_present from v_extensions where enabled = true and (extension = :identity or number_alias = :identity) order by extension",
    ['identity'=>$identity], 'all');
  $out['rows'] = is_array($rows) ? $rows : [];
}
elseif ($action === 'identities') {
  $rows = $database->select("select domain_uuid, extension, number_alias from v_extensions where enabled = true order by extension", null, 'all');
  $out['rows'] = is_array($rows) ? $rows : [];
}
else { fwrite(STDERR, "PBX_ACTION_UNSUPPORTED\n"); exit(5); }
echo json_encode($out, JSON_UNESCAPED_SLASHES) . PHP_EOL;
?>'''


class FusionPbxConfigProvider:
    """SELECT-only FusionPBX provider used by PBX-T004/M1."""

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        runner: PhpRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("PBX_PROVIDER_TIMEOUT_INVALID")
        self.profile = profile
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner or self._run_php

    def _run_php(
        self, script: str, request: dict[str, Any], timeout_seconds: float
    ) -> tuple[int | None, str]:
        path: str | None = None
        try:
            fd, path = tempfile.mkstemp(
                prefix="aivoip-pbx-read-", suffix=".php", dir="/tmp", text=True
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(script)
            cp = subprocess.run(
                [self.profile.php_bin, path],
                input=json.dumps(request, separators=(",", ":")),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return int(cp.returncode), cp.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        finally:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _call(self, action: str, **payload: Any) -> dict[str, Any]:
        request = {
            "fusionpbx_root": self.profile.fusionpbx_root,
            "action": action,
            **payload,
        }
        rc, output = self._runner(_READ_PROVIDER_PHP, request, self.timeout_seconds)
        if rc != 0 or not output.strip():
            raise FusionPbxConfigProviderError(f"PBX_PROVIDER_READ_FAILED:{action}")
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise FusionPbxConfigProviderError(
                f"PBX_PROVIDER_RESPONSE_INVALID:{action}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("mutation_executed") is not False
            or data.get("secret_values_emitted") is not False
        ):
            raise FusionPbxConfigProviderError("PBX_READ_ONLY_CONTRACT_BROKEN")
        return data

    @staticmethod
    def _validate_identity(identity: str) -> str:
        value = str(identity).strip()
        if not _EXTENSION_RE.fullmatch(value):
            raise FusionPbxConfigProviderError("PBX_EXTENSION_IDENTITY_INVALID")
        return value

    def discover_domains(self) -> tuple[FusionPbxDomain, ...]:
        rows = self._call("domains").get("rows") or []
        return tuple(
            FusionPbxDomain(
                domain_id=str(row.get("domain_uuid") or ""),
                domain_name=str(row.get("domain_name") or ""),
            )
            for row in rows
            if row.get("domain_uuid") and row.get("domain_name")
        )

    def get_extension(self, identity: str) -> tuple[FusionPbxExtensionView, ...]:
        target = self._validate_identity(identity)
        rows = self._call("extension", identity=target).get("rows") or []
        result: list[FusionPbxExtensionView] = []
        for row in rows:
            result.append(
                FusionPbxExtensionView(
                    domain_id=str(row.get("domain_uuid") or ""),
                    extension_uuid=str(row.get("extension_uuid") or ""),
                    extension=str(row.get("extension") or ""),
                    number_alias=(
                        str(row.get("number_alias"))
                        if row.get("number_alias") not in (None, "")
                        else None
                    ),
                    enabled=str(row.get("enabled")).lower()
                    in {"1", "true", "t", "yes", "on"},
                    description=(
                        str(row.get("description"))
                        if row.get("description") not in (None, "")
                        else None
                    ),
                    accountcode=(
                        str(row.get("accountcode"))
                        if row.get("accountcode") not in (None, "")
                        else None
                    ),
                    user_context=(
                        str(row.get("user_context"))
                        if row.get("user_context") not in (None, "")
                        else None
                    ),
                    password_present=str(row.get("password_present")).lower()
                    in {"1", "true", "t", "yes", "on"},
                )
            )
        return tuple(result)

    def extension_exists(self, identity: str) -> bool:
        return bool(self.get_extension(identity))

    def list_identities(self) -> dict[str, set[str]]:
        rows = self._call("identities").get("rows") or []
        by_domain: dict[str, set[str]] = {}
        for row in rows:
            domain = str(row.get("domain_uuid") or "")
            if not domain:
                continue
            bucket = by_domain.setdefault(domain, set())
            extension = str(row.get("extension") or "").strip()
            alias = str(row.get("number_alias") or "").strip()
            if extension:
                bucket.add(extension)
            if alias:
                bucket.add(alias)
        return by_domain

    def snapshot_extension(self, identity: str) -> tuple[FusionPbxExtensionView, ...]:
        return self.get_extension(identity)

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from datetime import datetime, timezone

from app.automation.adapters.pbx.base import SipRegistrationEvidence, TemporaryExtensionSpec


class FusionPbxSourceContractError(RuntimeError):
    pass


class FusionPbxOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedExtension:
    extension_uuid: str
    extension: str
    domain_name: str
    domain_identity_sha256: str


class FusionPbxLocalProvider:
    """Source-fenced local FusionPBX provider for isolated temporary extensions.

    The provider intentionally avoids an undocumented HTTP API. It loads the
    installed FusionPBX ``require.php`` and official ``database``/``extension``
    classes in local PHP, and mirrors only the create/delete shapes that are
    source-bound by the controlled-runner probes. Secrets travel to PHP only on
    stdin and are never included in stdout, exceptions, reprs, or persisted
    evidence.
    """

    EXPECTED_SOURCE_SHA256 = {
        "resources/require.php": "2d29ea99b786c5c111df4cfcc06319138f1544b30300f6aea70635f1100fd761",
        "resources/classes/database.php": "6a0b95eb29d1c27b24d4dcc4a8582b959c627a9173b973b19b2435e1e399dbbb",
        "app/extensions/resources/classes/extension.php": "842c070880ebe82cca676d8b04cda8377ca931180c91f72a499e813c3f3eaed9",
        "app/extensions/extension_copy.php": "5be1b1f491559553f78f1af2573d982a73583b1cafae54823fd0be42c2827d76",
    }

    _PHP = r'''<?php
    declare(strict_types=1);
    $root = getenv('FUSIONPBX_ROOT');
    if (!$root) { exit(2); }
    $raw = stream_get_contents(STDIN);
    $payload = json_decode($raw, true);
    if (!is_array($payload)) { exit(3); }
    $action = strval($payload['action'] ?? '');
    $target = strval($payload['extension'] ?? '');
    $extension_uuid = strval($payload['extension_uuid'] ?? '');
    if ($target === '' || $extension_uuid === '') { exit(4); }

    require_once $root . '/resources/require.php';
    require_once $root . '/app/extensions/resources/classes/extension.php';
    global $database;
    if (!$database) { exit(5); }

    $domains = $database->select(
        "select domain_uuid, domain_name from v_domains where domain_enabled = true order by domain_name asc",
        null,
        'all'
    );
    if (!is_array($domains) || count($domains) !== 1) { exit(6); }
    $domain_uuid = strval($domains[0]['domain_uuid'] ?? '');
    $domain_name = strval($domains[0]['domain_name'] ?? '');
    if ($domain_uuid === '' || $domain_name === '') { exit(7); }
    $extension = new extension([
        'database' => $database,
        'domain_uuid' => $domain_uuid,
        'domain_name' => $domain_name,
        'user_uuid' => '',
    ]);

    $safe = [
        'extension_uuid' => $extension_uuid,
        'extension' => $target,
        'domain_name' => $domain_name,
        'domain_identity_sha256' => hash('sha256', $domain_uuid . '|' . $domain_name),
        'secret_values_emitted' => false,
    ];

    if ($action === 'inspect') {
        $row = $database->select(
            "select extension_uuid, extension from v_extensions where domain_uuid = :domain_uuid and extension_uuid = :extension_uuid",
            ['domain_uuid' => $domain_uuid, 'extension_uuid' => $extension_uuid],
            'row'
        );
        $safe['uuid_present'] = is_array($row) && strval($row['extension_uuid'] ?? '') === $extension_uuid;
        $safe['target_present'] = $extension->exists($domain_uuid, $target);
        if ($safe['uuid_present']) {
            $safe['uuid_matches_target'] = strval($row['extension'] ?? '') === $target;
        }
        echo json_encode($safe, JSON_UNESCAPED_SLASHES) . PHP_EOL;
        exit(0);
    }

    if ($action === 'create') {
        $password = strval($payload['password'] ?? '');
        if ($password === '') { exit(8); }
        if ($extension->exists($domain_uuid, $target)) { exit(9); }
        $uuid_row = $database->select(
            "select extension_uuid from v_extensions where extension_uuid = :extension_uuid",
            ['extension_uuid' => $extension_uuid],
            'row'
        );
        if (is_array($uuid_row) && !empty($uuid_row['extension_uuid'])) { exit(10); }
        $array = [];
        $array['extensions'][0]['domain_uuid'] = $domain_uuid;
        $array['extensions'][0]['extension_uuid'] = $extension_uuid;
        $array['extensions'][0]['extension'] = $target;
        $array['extensions'][0]['number_alias'] = '';
        $array['extensions'][0]['password'] = $password;
        $array['extensions'][0]['accountcode'] = $target;
        $array['extensions'][0]['enabled'] = 'true';
        $database->save($array);
        unset($array, $password, $payload, $raw);
        $row = $database->select(
            "select extension_uuid, extension from v_extensions where domain_uuid = :domain_uuid and extension_uuid = :extension_uuid",
            ['domain_uuid' => $domain_uuid, 'extension_uuid' => $extension_uuid],
            'row'
        );
        $safe['uuid_present'] = is_array($row) && strval($row['extension_uuid'] ?? '') === $extension_uuid;
        $safe['target_present'] = $extension->exists($domain_uuid, $target);
        $safe['uuid_matches_target'] = $safe['uuid_present'] && strval($row['extension'] ?? '') === $target;
        if (!$safe['uuid_present'] || !$safe['target_present'] || !$safe['uuid_matches_target']) { exit(11); }
        echo json_encode($safe, JSON_UNESCAPED_SLASHES) . PHP_EOL;
        exit(0);
    }

    if ($action === 'delete') {
        $row = $database->select(
            "select extension_uuid, extension from v_extensions where domain_uuid = :domain_uuid and extension_uuid = :extension_uuid",
            ['domain_uuid' => $domain_uuid, 'extension_uuid' => $extension_uuid],
            'row'
        );
        if (!is_array($row) || empty($row['extension_uuid'])) {
            $safe['already_absent'] = true;
            $safe['target_present'] = $extension->exists($domain_uuid, $target);
            if ($safe['target_present']) { exit(12); }
            echo json_encode($safe, JSON_UNESCAPED_SLASHES) . PHP_EOL;
            exit(0);
        }
        if (strval($row['extension'] ?? '') !== $target) { exit(13); }
        $array = [];
        $array['extensions'][0]['extension_uuid'] = $extension_uuid;
        $database->delete($array);
        unset($array);
        $verify = $database->select(
            "select extension_uuid from v_extensions where domain_uuid = :domain_uuid and extension_uuid = :extension_uuid",
            ['domain_uuid' => $domain_uuid, 'extension_uuid' => $extension_uuid],
            'row'
        );
        $safe['uuid_present'] = is_array($verify) && !empty($verify['extension_uuid']);
        $safe['target_present'] = $extension->exists($domain_uuid, $target);
        if ($safe['uuid_present'] || $safe['target_present']) { exit(14); }
        echo json_encode($safe, JSON_UNESCAPED_SLASHES) . PHP_EOL;
        exit(0);
    }
    exit(15);
    ?>'''

    def __init__(
        self,
        *,
        root: str | Path = "/var/www/fusionpbx",
        php_bin: str = "php",
        fs_cli_bin: str = "fs_cli",
        subprocess_timeout_seconds: float = 20.0,
    ) -> None:
        self.root = Path(root)
        self.php_bin = php_bin
        self.fs_cli_bin = fs_cli_bin
        self.subprocess_timeout_seconds = float(subprocess_timeout_seconds)

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def assert_source_contract(self) -> None:
        if shutil.which(self.php_bin) is None:
            raise FusionPbxSourceContractError("FUSIONPBX_PHP_REQUIRED")
        if shutil.which(self.fs_cli_bin) is None:
            raise FusionPbxSourceContractError("FUSIONPBX_FS_CLI_REQUIRED")
        for relative, expected in self.EXPECTED_SOURCE_SHA256.items():
            path = self.root / relative
            if not path.is_file() or self._sha256(path) != expected:
                raise FusionPbxSourceContractError(f"FUSIONPBX_SOURCE_FENCE_FAILED:{relative}")

    def _php_payload(self, payload: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
        self.assert_source_contract()
        # php reads the program from fd 3 while STDIN remains exclusively the
        # secret-bearing JSON payload. Neither secret nor password appears in
        # argv, environment, stdout, stderr, or exception text.
        import os
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, self._PHP.encode("utf-8"))
        finally:
            os.close(write_fd)
        try:
            cp = subprocess.run(
                [self.php_bin, f"/dev/fd/{read_fd}"],
                input=json.dumps(dict(payload), ensure_ascii=False),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env={**os.environ, "FUSIONPBX_ROOT": str(self.root)},
                pass_fds=(read_fd,),
                timeout=self.subprocess_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise FusionPbxOperationError(f"FUSIONPBX_{operation}_RESULT_UNKNOWN") from None
        finally:
            os.close(read_fd)
        if cp.returncode != 0:
            raise FusionPbxOperationError(f"FUSIONPBX_{operation}_FAILED:RC{cp.returncode}")
        try:
            data = json.loads(cp.stdout)
        except json.JSONDecodeError:
            raise FusionPbxOperationError(f"FUSIONPBX_{operation}_OUTPUT_INVALID") from None
        if not isinstance(data, dict) or data.get("secret_values_emitted") is not False:
            raise FusionPbxOperationError(f"FUSIONPBX_{operation}_SAFE_OUTPUT_REQUIRED")
        return data

    def _reload_xml(self) -> None:
        try:
            cp = subprocess.run(
                [self.fs_cli_bin, "-x", "reloadxml"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.subprocess_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise FusionPbxOperationError("FUSIONPBX_RELOADXML_RESULT_UNKNOWN") from None
        if cp.returncode != 0:
            raise FusionPbxOperationError(f"FUSIONPBX_RELOADXML_FAILED:RC{cp.returncode}")

    def inspect(self, spec: TemporaryExtensionSpec) -> dict[str, Any]:
        return self._php_payload(
            {
                "action": "inspect",
                "extension": spec.extension,
                "extension_uuid": spec.extension_uuid,
            },
            operation="INSPECT",
        )

    def create(self, spec: TemporaryExtensionSpec) -> PreparedExtension:
        # The spec must be installed in gate runtime before this function is
        # called so cleanup can observe-before-cleanup even on UNKNOWN.
        data = self._php_payload(
            {
                "action": "create",
                "extension": spec.extension,
                "extension_uuid": spec.extension_uuid,
                "password": spec.password,
            },
            operation="CREATE",
        )
        self._reload_xml()
        return PreparedExtension(
            extension_uuid=spec.extension_uuid,
            extension=spec.extension,
            domain_name=str(data.get("domain_name") or ""),
            domain_identity_sha256=str(data.get("domain_identity_sha256") or ""),
        )

    def delete(self, spec: TemporaryExtensionSpec) -> dict[str, Any]:
        # Observe first. A prior create transport failure is UNKNOWN, never a
        # reason to blindly create/retry. Cleanup only removes our exact UUID.
        before = self.inspect(spec)
        if before.get("uuid_present") and before.get("uuid_matches_target") is not True:
            raise FusionPbxOperationError("FUSIONPBX_CLEANUP_UUID_TARGET_MISMATCH")
        if not before.get("uuid_present"):
            if before.get("target_present"):
                raise FusionPbxOperationError("FUSIONPBX_CLEANUP_FOREIGN_TARGET_PRESENT")
            return {"already_absent": True, "secret_values_emitted": False}
        data = self._php_payload(
            {
                "action": "delete",
                "extension": spec.extension,
                "extension_uuid": spec.extension_uuid,
            },
            operation="DELETE",
        )
        self._reload_xml()
        return data

    def verify_absent(self, spec: TemporaryExtensionSpec) -> tuple[bool, dict[str, Any]]:
        data = self.inspect(spec)
        absent = not bool(data.get("uuid_present")) and not bool(data.get("target_present"))
        return absent, {
            "pbx_extension_absent": absent,
            "extension": spec.extension,
            "secret_values_emitted": False,
        }

    def _sofia_contact(self, *, number: str, domain_name: str) -> bool:
        command = f"sofia_contact {number}@{domain_name}"
        try:
            cp = subprocess.run(
                [self.fs_cli_bin, "-x", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self.subprocess_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        output = cp.stdout.strip()
        return cp.returncode == 0 and bool(output) and not output.lower().startswith("error/")

    async def wait_registered(self, *, number: str, timeout_seconds: float) -> SipRegistrationEvidence:
        deadline = time.monotonic() + float(timeout_seconds)
        # Resolve the one active domain without exposing UUIDs or secrets.
        placeholder = TemporaryExtensionSpec(extension_uuid="00000000-0000-4000-8000-000000000000", extension=number, password="")
        while True:
            data = await asyncio.to_thread(self.inspect, placeholder)
            domain_name = str(data.get("domain_name") or "")
            registered = bool(domain_name) and await asyncio.to_thread(
                self._sofia_contact, number=number, domain_name=domain_name
            )
            if registered:
                return SipRegistrationEvidence(
                    registered=True,
                    number=number,
                    evidence_refs=(f"fusionpbx-runtime://sofia-contact/{number}",),
                    source_timestamp=datetime.now(timezone.utc),
                    details={"provider": "fusionpbx_fs_cli", "registered": True},
                )
            if time.monotonic() >= deadline:
                return SipRegistrationEvidence(
                    registered=False,
                    number=number,
                    evidence_refs=(f"fusionpbx-runtime://sofia-contact/{number}",),
                    source_timestamp=datetime.now(timezone.utc),
                    details={"provider": "fusionpbx_fs_cli", "registered": False},
                )
            await asyncio.sleep(2.0)

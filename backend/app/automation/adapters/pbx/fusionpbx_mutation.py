from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import PbxLeaseToken
from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    normalize_testlab_provider_mutation_identity,
    normalize_dial_alias,
)
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence


class PbxTokenValidator(Protocol):
    def validate(self, token: PbxLeaseToken) -> bool: ...


MutationRunner = Callable[[str, dict[str, Any], float], tuple[int | None, str]]


CacheReconcileRunner = Callable[[tuple[str, ...], float], bool]


class FusionPbxMutationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FusionPbxMutationResult:
    status: str
    operation: str
    extension: str
    extension_uuid: str | None = None
    ownership_marker: str | None = None
    runtime_reload_requested: bool = False

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED"

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "extension": self.extension,
            "extension_uuid": self.extension_uuid,
            "ownership_marker": self.ownership_marker,
            "runtime_reload_requested": self.runtime_reload_requested,
            "mutation_executed": self.status in {"CONFIRMED", "UNKNOWN"},
            "secret_values_emitted": False,
        }


_CACHE_RECONCILE_LUA = r'''
local Cache = require "resources.functions.cache"
local function hex_decode(value)
  if value == nil or string.len(value) % 2 ~= 0 or string.match(value, "[^0-9A-Fa-f]") then return nil end
  return (string.gsub(value, "..", function(cc) return string.char(tonumber(cc, 16)) end))
end
local ok_all = true
for i = 1, #argv do
  local key = hex_decode(argv[i])
  if key == nil then ok_all = false else
    local ok = Cache.del(key)
    if not ok then ok_all = false end
  end
end
local api = freeswitch.API()
api:execute("reloadxml", "")
if ok_all then stream:write("OK") else stream:write("ERROR") end
'''


_MUTATION_PHP = r'''<?php
declare(strict_types=1);
$req = json_decode(stream_get_contents(STDIN), true);
if (!is_array($req)) { fwrite(STDERR, "PBX_REQUEST_INVALID\n"); exit(2); }
$root = strval($req['fusionpbx_root'] ?? '');
if ($root === '' || $root[0] !== '/') { fwrite(STDERR, "PBX_ROOT_INVALID\n"); exit(3); }
require_once $root . '/resources/require.php';
require_once $root . '/app/extensions/resources/classes/extension.php';
global $database;
if (!$database) { fwrite(STDERR, "PBX_DATABASE_UNAVAILABLE\n"); exit(4); }
$operation = strval($req['operation'] ?? '');
$domain_uuid = strval($req['domain_uuid'] ?? '');
$domain_name = strval($req['domain_name'] ?? '');
$extension = strval($req['extension'] ?? '');
$marker = strval($req['ownership_marker'] ?? '');
if ($domain_uuid === '' || $domain_name === '' || $extension === '') { fwrite(STDERR, "PBX_IDENTITY_REQUIRED\n"); exit(5); }
$ext = new extension(['database'=>$database,'domain_uuid'=>$domain_uuid,'domain_name'=>$domain_name,'user_uuid'=>'']);

if ($operation === 'create') {
  $password = strval($req['password'] ?? '');
  $template = strval($req['template_identity'] ?? '');
  $dial_alias = strval($req['dial_alias'] ?? '');
  if ($password === '' || $marker === '' || $template === '') { fwrite(STDERR, "PBX_CREATE_INPUT_REQUIRED\n"); exit(6); }
  if ($ext->exists($domain_uuid, $extension)) { fwrite(STDERR, "PBX_EXTENSION_ALREADY_EXISTS\n"); exit(7); }
  $row = $database->select(
    "select directory_visible, directory_exten_visible, limit_max, limit_destination, user_context, toll_allow, call_timeout, call_group, user_record, hold_music, auth_acl, cidr, sip_force_contact, nibble_account, sip_force_expires, mwi_account, sip_bypass_media, dial_string, extension_type, cast(enabled as text) enabled from v_extensions where domain_uuid = :domain_uuid and enabled = true and (extension = :template or number_alias = :template) limit 1",
    ['domain_uuid'=>$domain_uuid,'template'=>$template], 'row');
  if (!is_array($row)) { fwrite(STDERR, "PBX_TEMPLATE_NOT_FOUND\n"); exit(8); }
  $extension_uuid = uuid();
  $array['extensions'][0]['domain_uuid'] = $domain_uuid;
  $array['extensions'][0]['extension_uuid'] = $extension_uuid;
  $array['extensions'][0]['extension'] = $extension;
  $array['extensions'][0]['number_alias'] = ($dial_alias === '' ? null : $dial_alias);
  $array['extensions'][0]['password'] = $password;
  $array['extensions'][0]['accountcode'] = $extension;
  $array['extensions'][0]['effective_caller_id_name'] = 'AIVOIP ' . $extension;
  $array['extensions'][0]['effective_caller_id_number'] = $extension;
  $array['extensions'][0]['outbound_caller_id_name'] = 'AIVOIP ' . $extension;
  $array['extensions'][0]['outbound_caller_id_number'] = $extension;
  foreach (['directory_visible','directory_exten_visible','limit_max','limit_destination','user_context','toll_allow','call_timeout','call_group','user_record','hold_music','auth_acl','cidr','sip_force_contact','nibble_account','sip_force_expires','mwi_account','sip_bypass_media','dial_string','extension_type','enabled'] as $field) {
    $array['extensions'][0][$field] = $row[$field] ?? null;
  }
  $array['extensions'][0]['description'] = $marker;
  $p = permissions::new();
  $p->add('extension_add', 'temp');
  try {
    $ok = $database->save($array);
  } finally {
    $p->delete('extension_add', 'temp');
  }
  if ($ok === false) { fwrite(STDERR, "PBX_CREATE_SAVE_FAILED\n"); exit(9); }
  $ext->xml();
  event_socket::api('reloadxml');
  echo json_encode([
    'status'=>'CONFIRMED','operation'=>'create','extension'=>$extension,
    'extension_uuid'=>$extension_uuid,'ownership_marker'=>$marker,
    'runtime_reload_requested'=>true,'secret_values_emitted'=>false
  ], JSON_UNESCAPED_SLASHES) . PHP_EOL;
  exit(0);
}

if ($operation === 'reconcile_absent_cache') {
  $user_context = strval($req['user_context'] ?? '');
  if ($marker === '' || $user_context === '') { fwrite(STDERR, "PBX_CACHE_RECONCILE_INPUT_REQUIRED\n"); exit(15); }
  $cache = new cache;
  // FusionPBX Lua XML handler caches directory entries by domain_name.
  // Native extension deletion uses user_context, which may be "default".
  // Invalidate both identities so runtime cannot retain a deleted user.
  $cache->delete('directory:' . $extension . '@' . $domain_name);
  if ($user_context !== $domain_name) {
    $cache->delete('directory:' . $extension . '@' . $user_context);
  }
  event_socket::api('reloadxml');
  echo json_encode([
    'status'=>'CONFIRMED','operation'=>'reconcile_absent_cache','extension'=>$extension,
    'extension_uuid'=>null,'ownership_marker'=>$marker,
    'runtime_reload_requested'=>true,'secret_values_emitted'=>false
  ], JSON_UNESCAPED_SLASHES) . PHP_EOL;
  exit(0);
}

if ($operation === 'delete') {
  $row = $database->select(
    "select extension_uuid, description, number_alias, user_context from v_extensions where domain_uuid = :domain_uuid and extension = :extension limit 1",
    ['domain_uuid'=>$domain_uuid,'extension'=>$extension], 'row');
  if (!is_array($row)) { fwrite(STDERR, "PBX_EXTENSION_NOT_FOUND\n"); exit(10); }
  if (strval($row['description'] ?? '') !== $marker || $marker === '') { fwrite(STDERR, "PBX_RESOURCE_OWNERSHIP_UNPROVEN\n"); exit(11); }
  $extension_uuid = strval($row['extension_uuid'] ?? '');
  if ($extension_uuid === '') { fwrite(STDERR, "PBX_EXTENSION_UUID_REQUIRED\n"); exit(12); }
  $array['extensions'][0]['extension_uuid'] = $extension_uuid;
  $p = permissions::new();
  $p->add('extension_delete', 'temp');
  try {
    $ok = $database->delete($array);
  } finally {
    $p->delete('extension_delete', 'temp');
  }
  if ($ok === false) { fwrite(STDERR, "PBX_DELETE_FAILED\n"); exit(13); }
  $user_context = strval($row['user_context'] ?? '');
  $alias = strval($row['number_alias'] ?? '');
  $cache = new cache;
  $cache->delete('directory:' . $extension . '@' . $domain_name);
  if ($user_context !== '' && $user_context !== $domain_name) {
    $cache->delete('directory:' . $extension . '@' . $user_context);
  }
  if ($alias !== '') {
    $cache->delete('directory:' . $alias . '@' . $domain_name);
    if ($user_context !== '' && $user_context !== $domain_name) {
      $cache->delete('directory:' . $alias . '@' . $user_context);
    }
  }
  $ext->xml();
  event_socket::api('reloadxml');
  echo json_encode([
    'status'=>'CONFIRMED','operation'=>'delete','extension'=>$extension,
    'extension_uuid'=>$extension_uuid,'ownership_marker'=>$marker,
    'runtime_reload_requested'=>true,'secret_values_emitted'=>false
  ], JSON_UNESCAPED_SLASHES) . PHP_EOL;
  exit(0);
}

fwrite(STDERR, "PBX_OPERATION_UNSUPPORTED\n"); exit(14);
?>'''


class FusionPbxMutationProvider:
    """Lease-fenced native FusionPBX mutation provider.

    Any transport/process ambiguity after invocation is UNKNOWN, never an
    automatic retry. Callers must observe current PBX state first.
    """

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        authority: PbxTokenValidator,
        source_fence: FusionPbxSourceFence | None = None,
        runner: MutationRunner | None = None,
        cache_reconcile_runner: CacheReconcileRunner | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.profile = profile
        self.authority = authority
        self.source_fence = source_fence or FusionPbxSourceFence(profile)
        self._runner = runner or self._run_php
        self._cache_reconcile_runner = cache_reconcile_runner or self._run_cache_reconcile_lua
        self.timeout_seconds = float(timeout_seconds)

    def _run_php(
        self, script: str, request: dict[str, Any], timeout_seconds: float
    ) -> tuple[int | None, str]:
        path: str | None = None
        try:
            fd, path = tempfile.mkstemp(
                prefix="aivoip-pbx-mutate-", suffix=".php", dir="/tmp", text=True
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

    def _run_cache_reconcile_lua(self, keys: tuple[str, ...], timeout_seconds: float) -> bool:
        if not keys:
            return False
        path: str | None = None
        try:
            fd, path = tempfile.mkstemp(
                prefix="aivoip-pbx-cache-", suffix=".lua", dir="/tmp", text=True
            )
            # FreeSWITCH runs as www-data and must be able to read this secret-free helper.
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_CACHE_RECONCILE_LUA)
            encoded_keys = tuple(key.encode("utf-8").hex() for key in keys)
            command = "lua " + path + " " + " ".join(encoded_keys)
            cp = subprocess.run(
                [self.profile.fs_cli_bin, "-x", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return cp.returncode == 0 and (cp.stdout or "").strip() == "OK"
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _cache_identity(value: str, *, code: str) -> str:
        import re
        if not value or len(value) > 255 or re.fullmatch(r"[A-Za-z0-9_.+:-]+", value) is None:
            raise FusionPbxMutationError(code)
        return value

    def _preflight(self, token: PbxLeaseToken) -> None:
        if not self.authority.validate(token):
            raise FusionPbxMutationError("PBX_LEASE_FENCED")
        if token.extension in self.profile.protected_extensions:
            raise FusionPbxMutationError("PBX_RESOURCE_PROTECTED")
        try:
            normalize_testlab_provider_mutation_identity(token.extension)
        except PbxExtensionIdentityError as exc:
            raise FusionPbxMutationError("PBX_PROVIDER_IDENTITY_INVALID") from exc
        self.source_fence.verify_mutation_contract()

    @staticmethod
    def _marker(token: PbxLeaseToken) -> str:
        return f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"

    def _invoke(
        self, operation: str, token: PbxLeaseToken, *, ownership_marker: str | None = None, **request: Any
    ) -> FusionPbxMutationResult:
        self._preflight(token)
        marker = ownership_marker or self._marker(token)
        if not marker.startswith("AIVOIP_AUTOMATION:"):
            raise FusionPbxMutationError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
        payload = {
            "fusionpbx_root": self.profile.fusionpbx_root,
            "operation": operation,
            "domain_uuid": token.domain_id,
            "extension": token.extension,
            "ownership_marker": marker,
            **request,
        }
        rc, output = self._runner(_MUTATION_PHP, payload, self.timeout_seconds)
        if rc != 0 or not output.strip():
            return FusionPbxMutationResult(
                status="UNKNOWN",
                operation=operation,
                extension=token.extension,
                ownership_marker=marker,
            )
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return FusionPbxMutationResult(
                status="UNKNOWN",
                operation=operation,
                extension=token.extension,
                ownership_marker=marker,
            )
        if data.get("secret_values_emitted") is not False or data.get("status") != "CONFIRMED":
            return FusionPbxMutationResult(
                status="UNKNOWN",
                operation=operation,
                extension=token.extension,
                ownership_marker=marker,
            )
        if data.get("extension") != token.extension or data.get("ownership_marker") != marker:
            return FusionPbxMutationResult(
                status="UNKNOWN",
                operation=operation,
                extension=token.extension,
                ownership_marker=marker,
            )
        return FusionPbxMutationResult(
            status="CONFIRMED",
            operation=operation,
            extension=token.extension,
            extension_uuid=str(data.get("extension_uuid") or "") or None,
            ownership_marker=marker,
            runtime_reload_requested=bool(data.get("runtime_reload_requested")),
        )

    def create_extension(
        self,
        token: PbxLeaseToken,
        *,
        domain_name: str,
        password: str,
        template_identity: str,
        dial_alias: str | None = None,
    ) -> FusionPbxMutationResult:
        if not password or len(password) < 12:
            raise FusionPbxMutationError("PBX_EXTENSION_PASSWORD_INVALID")
        if template_identity not in self.profile.protected_extensions:
            raise FusionPbxMutationError("PBX_TEMPLATE_NOT_PROTECTED")
        if dial_alias is not None:
            try:
                dial_alias = normalize_dial_alias(dial_alias)
            except PbxExtensionIdentityError as exc:
                raise FusionPbxMutationError(str(exc)) from exc
        return self._invoke(
            "create",
            token,
            domain_name=domain_name,
            password=password,
            template_identity=template_identity,
            dial_alias=dial_alias,
        )

    def delete_extension(
        self, token: PbxLeaseToken, *, domain_name: str, ownership_marker: str | None = None
    ) -> FusionPbxMutationResult:
        return self._invoke(
            "delete", token, domain_name=domain_name, ownership_marker=ownership_marker
        )

    def reconcile_absent_extension_cache(
        self,
        token: PbxLeaseToken,
        *,
        domain_name: str,
        user_context: str,
        dial_alias: str | None = None,
        ownership_marker: str | None = None,
    ) -> FusionPbxMutationResult:
        self._preflight(token)
        domain_name = self._cache_identity(domain_name, code="PBX_DOMAIN_INVALID")
        user_context = self._cache_identity(user_context, code="PBX_USER_CONTEXT_INVALID")
        if dial_alias is not None:
            try:
                dial_alias = normalize_dial_alias(dial_alias)
            except PbxExtensionIdentityError as exc:
                raise FusionPbxMutationError(str(exc)) from exc
        marker = ownership_marker or self._marker(token)
        if not marker.startswith("AIVOIP_AUTOMATION:"):
            raise FusionPbxMutationError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
        keys = [f"directory:{token.extension}@{domain_name}"]
        if user_context != domain_name:
            keys.append(f"directory:{token.extension}@{user_context}")
        if dial_alias:
            keys.append(f"directory:{dial_alias}@{domain_name}")
            if user_context != domain_name:
                keys.append(f"directory:{dial_alias}@{user_context}")
        ok = self._cache_reconcile_runner(tuple(keys), self.timeout_seconds)
        return FusionPbxMutationResult(
            status="CONFIRMED" if ok else "UNKNOWN",
            operation="reconcile_absent_cache",
            extension=token.extension,
            extension_uuid=None,
            ownership_marker=marker,
            runtime_reload_requested=ok,
        )

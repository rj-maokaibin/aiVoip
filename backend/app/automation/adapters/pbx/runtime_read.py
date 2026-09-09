from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from typing import Callable

from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.identity import PbxExtensionIdentityError, normalize_sip_user_wire_identity


RuntimeRunner = Callable[[tuple[str, ...], float], tuple[int | None, str]]
_SAFE_API_LUA = r'''
local function hex_decode(value)
  if value == nil or string.len(value) % 2 ~= 0 or string.match(value, "[^0-9A-Fa-f]") then return nil end
  return (string.gsub(value, "..", function(cc) return string.char(tonumber(cc, 16)) end))
end
local op = argv[1]
local identity = hex_decode(argv[2])
local domain = hex_decode(argv[3])
if identity == nil or domain == nil then stream:write("-ERR") return end
local api = freeswitch.API()
if op == "exists" then
  stream:write(api:execute("user_exists", "id " .. identity .. " " .. domain) or "")
elseif op == "context" then
  stream:write(api:execute("user_data", identity .. "@" .. domain .. " var user_context") or "")
elseif op == "find" then
  stream:write(api:execute("find_user_xml", "id " .. identity .. " " .. domain) or "")
else
  stream:write("-ERR")
end
'''
_DOMAIN_RE = re.compile(r"^[0-9A-Za-z._:-]{1,255}$")


class FreeSwitchRuntimeReadError(RuntimeError):
    pass


class FreeSwitchRuntimeReadProbe:
    """Read-only FreeSWITCH directory/runtime reconcile used before ESL M4.

    `user_exists` is authoritative for exact directory visibility on the current
    lab. `list_users` remains auxiliary because this FreeSWITCH build can return
    an empty list even while exact `user_exists` is true.
    """

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        runner: RuntimeRunner | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.profile = profile
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner or self._run
        self._uses_default_runner = runner is None

    def _run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int | None, str]:
        if shutil.which(argv[0]) is None:
            return None, ""
        try:
            cp = subprocess.run(
                list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        return int(cp.returncode), cp.stdout or ""

    def _safe_api(self, operation: str, identity: str, domain_name: str) -> tuple[int | None, str]:
        path: str | None = None
        try:
            fd, path = tempfile.mkstemp(prefix="aivoip-fs-read-", suffix=".lua", dir="/tmp", text=True)
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_SAFE_API_LUA)
            command = "lua " + path + " " + operation + " " + identity.encode("utf-8").hex() + " " + domain_name.encode("utf-8").hex()
            return self._run((self.profile.fs_cli_bin, "-x", command), self.timeout_seconds)
        finally:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _api(self, operation: str, identity: str, domain_name: str, direct_command: str) -> tuple[int | None, str]:
        if self._uses_default_runner:
            return self._safe_api(operation, identity, domain_name)
        return self._runner((self.profile.fs_cli_bin, "-x", direct_command), self.timeout_seconds)

    @staticmethod
    def _parse_first_field(output: str) -> set[str]:
        result: set[str] = set()
        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("-") or line.startswith("userid|") or line in {"+OK", "-ERR"}:
                continue
            first = line.split("|", 1)[0].strip()
            try:
                result.add(normalize_sip_user_wire_identity(first))
            except PbxExtensionIdentityError:
                continue
        return result

    def list_users(self) -> set[str]:
        rc, output = self._runner((self.profile.fs_cli_bin, "-x", "list_users"), self.timeout_seconds)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_LIST_USERS_FAILED")
        return self._parse_first_field(output)

    def user_visible(self, identity: str, *, domain_name: str) -> bool:
        try:
            identity = normalize_sip_user_wire_identity(identity)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID") from exc
        domain_name = str(domain_name).strip()
        if not _DOMAIN_RE.fullmatch(domain_name):
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID")
        command = f"user_exists id {identity} {domain_name}"
        rc, output = self._api("exists", identity, domain_name, command)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_EXISTS_FAILED")
        normalized = (output or "").strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_EXISTS_INVALID")

    def user_context(self, identity: str, *, domain_name: str) -> str | None:
        try:
            identity = normalize_sip_user_wire_identity(identity)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID") from exc
        domain_name = str(domain_name).strip()
        if not _DOMAIN_RE.fullmatch(domain_name):
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID")
        command = f"user_data {identity}@{domain_name} var user_context"
        rc, output = self._api("context", identity, domain_name, command)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_DATA_FAILED")
        value = (output or "").strip()
        if value in {"", "_undef_", "-ERR"}:
            return None
        if not re.fullmatch(r"[0-9A-Za-z_.+-]{1,128}", value):
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_CONTEXT_INVALID")
        return value

    def resolve_directory_identity(self, identity: str, *, domain_name: str) -> dict[str, str | None] | None:
        try:
            identity = normalize_sip_user_wire_identity(identity)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID") from exc
        domain_name = str(domain_name).strip()
        if not _DOMAIN_RE.fullmatch(domain_name):
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID")
        command = f"find_user_xml id {identity} {domain_name}"
        rc, output = self._api("find", identity, domain_name, command)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_FIND_USER_FAILED")
        text = (output or "").strip()
        if not text or text.startswith("-ERR"):
            return None
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_FIND_USER_XML_INVALID") from exc
        if root.tag != "user":
            user = root.find(".//user")
            if user is None:
                return None
        else:
            user = root
        resolved_id = str(user.attrib.get("id") or "").strip()
        number_alias = str(user.attrib.get("number-alias") or "").strip() or None
        resolved_domain = str(user.attrib.get("domain-name") or domain_name).strip()
        try:
            resolved_id = normalize_sip_user_wire_identity(resolved_id)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_FIND_USER_IDENTITY_INVALID") from exc
        if not _DOMAIN_RE.fullmatch(resolved_domain):
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_FIND_USER_IDENTITY_INVALID")
        return {
            "resolved_identity": resolved_id,
            "number_alias": number_alias,
            "domain_name": resolved_domain,
            "secret_values_emitted": False,
        }

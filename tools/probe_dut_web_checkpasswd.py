#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec

_TOOL_CANDIDATES = (
    "/usr/lib/lua/luci/utils/tool.lua",
    "/rom/usr/lib/lua/luci/utils/tool.lua",
)
_NOAUTH = "/usr/lib/lua/luci/modules/noauth.lua"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]*$")
_SAFE_MODULE = re.compile(r"^[A-Za-z0-9_.+:-]+$")
_SAFE_UCI_LITERAL = re.compile(r"^[A-Za-z0-9_@.\[\]-]{2,120}$")
_SAFE_PATH = re.compile(r"^/(?:etc|tmp|usr|rom|www|lib)(?:/|$)")
_INTERESTING = re.compile(
    r"auth|login|pass|pwd|user|account|shadow|crypt|decrypt|encrypt|aes|md5|base64|uci|config|exec|system|check|verify",
    re.IGNORECASE,
)
_SAFE_LITERALS = (
    "admin", "root", "username", "user", "password", "passwd", "pwd",
    "luci", "flash_keep", "auth", "login", "encry",
)


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _function_params(source: str) -> list[str]:
    patterns = (
        r"function\s+(?:[A-Za-z_][A-Za-z0-9_.:]*[.:])?checkPasswd\s*\(([^)]*)\)",
        r"checkPasswd\s*=\s*function\s*\(([^)]*)\)",
    )
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            values = []
            for raw in match.group(1).split(","):
                item = raw.strip()
                if _SAFE_IDENTIFIER.fullmatch(item):
                    values.append(item)
            return values
    return []


def _call_identifiers(source: str) -> list[str]:
    found = set()
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.:]*)\s*\(", source):
        name = match.group(1)
        if _SAFE_IDENTIFIER.fullmatch(name) and _INTERESTING.search(name):
            found.add(name)
    return sorted(found)[:100]


def _modules(source: str) -> list[str]:
    found = set()
    for match in re.finditer(r"require\s*(?:\(\s*)?['\"]([A-Za-z0-9_.+:-]+)['\"]", source):
        value = match.group(1)
        if _SAFE_MODULE.fullmatch(value):
            found.add(value)
    return sorted(found)[:100]


def _safe_string_literals(source: str) -> tuple[list[str], list[str]]:
    uci = set()
    paths = set()
    for match in re.finditer(r"['\"]([^'\"\r\n]{1,160})['\"]", source):
        value = match.group(1)
        if _SAFE_PATH.match(value):
            paths.add(value)
        if "." in value and _SAFE_UCI_LITERAL.fullmatch(value):
            uci.add(value)
    return sorted(uci)[:100], sorted(paths)[:100]


def _classify_call_args(source: str) -> list[dict]:
    match = re.search(r"tool\.checkPasswd\s*\(([^\n)]*)\)", source)
    if not match:
        return []
    rows = []
    for index, raw in enumerate(match.group(1).split(",")):
        token = raw.strip()
        row = {"index": index, "kind": "other"}
        if _SAFE_IDENTIFIER.fullmatch(token):
            row = {"index": index, "kind": "identifier", "identifier": token}
        else:
            literal = re.fullmatch(r"['\"]([^'\"]+)['\"]", token)
            if literal and literal.group(1) in _SAFE_LITERALS:
                row = {"index": index, "kind": "safe_literal", "literal": literal.group(1)}
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:]*\s*\(.*\)", token):
                name = token.split("(", 1)[0].strip()
                row = {"index": index, "kind": "call", "identifier": name if _SAFE_IDENTIFIER.fullmatch(name) else "<redacted>"}
        rows.append(row)
    return rows


def _structure(source: str) -> dict:
    uci_literals, file_paths = _safe_string_literals(source)
    return {
        "source_sha256": hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest(),
        "source_bytes": len(source.encode("utf-8", errors="ignore")),
        "checkpasswd_found": bool(re.search(r"checkPasswd", source)),
        "checkpasswd_parameter_names": _function_params(source),
        "interesting_call_identifiers": _call_identifiers(source),
        "require_modules": _modules(source),
        "safe_uci_like_literals": uci_literals,
        "safe_file_path_literals": file_paths,
        "safe_literal_flags": {
            value: bool(re.search(r"['\"]" + re.escape(value) + r"['\"]", source))
            for value in _SAFE_LITERALS
        },
        "flash_keep_passwd_literal": bool(re.search(r"flash_keep[^\n]{0,120}passwd|passwd[^\n]{0,120}flash_keep", source)),
        "source_lines_emitted": False,
        "arbitrary_scalar_values_emitted": False,
    }


async def _probe(args) -> dict:
    _env_password(args.password_env)
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        tool_path = ""
        tool_source = ""
        for candidate in _TOOL_CANDIDATES:
            cp = await adapter.execute_shell("cat '" + candidate + "' 2>/dev/null", timeout=args.timeout, retries=1)
            if cp.exit_status == 0 and (cp.stdout or ""):
                tool_path = candidate
                tool_source = cp.stdout or ""
                break
        if not tool_source:
            raise RuntimeError("CHECKPASSWD_SOURCE_NOT_FOUND")
        noauth_cp = await adapter.execute_shell("cat '" + _NOAUTH + "' 2>/dev/null", timeout=args.timeout, retries=1)
        if noauth_cp.exit_status != 0 or not (noauth_cp.stdout or ""):
            raise RuntimeError("NOAUTH_SOURCE_READ_FAILED")
        noauth_source = noauth_cp.stdout or ""
        accounts_cp = await adapter.execute_shell(
            "cut -d: -f1 /etc/passwd 2>/dev/null | head -n 200",
            timeout=args.timeout,
            retries=1,
        )
        accounts = {line.strip() for line in (accounts_cp.stdout or "").splitlines() if line.strip()}
    finally:
        await adapter.disconnect()

    structure = _structure(tool_source)
    return {
        "schema": "dut-web-checkpasswd-source-v1",
        "read_only": True,
        "mutation_executed": False,
        "secret_values_emitted": False,
        "secret_values_persisted": False,
        "tool_source_path": tool_path,
        "tool_structure": structure,
        "noauth_call": {
            "tool_checkpasswd_called": bool(re.search(r"tool\.checkPasswd\s*\(", noauth_source)),
            "argument_classes": _classify_call_args(noauth_source),
            "source_sha256": hashlib.sha256(noauth_source.encode("utf-8", errors="ignore")).hexdigest(),
            "source_lines_emitted": False,
        },
        "system_accounts": {
            "root_present": "root" in accounts,
            "admin_present": "admin" in accounts,
            "account_count": len(accounts),
            "password_hashes_read": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only source binding for current DUT WEB tool.checkPasswd")
    p.add_argument("--device-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--username", default="root")
    p.add_argument("--platform-id", default=None)
    p.add_argument("--password-env", required=True)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    payload = asyncio.run(_probe(args))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    structure = payload["tool_structure"]
    print(json.dumps({
        "DUT_WEB_CHECKPASSWD_SOURCE_PROBE": "PASS",
        "checkpasswd_found": structure["checkpasswd_found"],
        "parameter_count": len(structure["checkpasswd_parameter_names"]),
        "call_count": len(structure["interesting_call_identifiers"]),
        "flash_keep_passwd_literal": structure["flash_keep_passwd_literal"],
        "noauth_call_arg_count": len(payload["noauth_call"]["argument_classes"]),
        "root_present": payload["system_accounts"]["root_present"],
        "admin_present": payload["system_accounts"]["admin_present"],
        "mutation": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

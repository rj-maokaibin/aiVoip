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

_TARGETS = (
    "/usr/lib/lua/luci/dispatcher.lua",
    "/usr/lib/lua/luci/modules/noauth.lua",
    "/usr/lib/lua/luci/modules/env.lua",
    "/usr/lib/lua/luci/view/login.htm",
)
_NOAUTH = "/usr/lib/lua/luci/modules/noauth.lua"

# Boolean source facts only. We never return matching source lines or scalar values.
_PATTERNS = {
    "api_auth_literal": r"/cgi-bin/luci/api/auth|api[^A-Za-z0-9_]+auth|auth[^A-Za-z0-9_]+api",
    "request_username": r"formvalue[^\n]*(username|user)|username[^\n]*formvalue",
    "request_password": r"formvalue[^\n]*(password|passwd|pwd)|password[^\n]*formvalue",
    "sys_checkpasswd": r"(luci\.)?sys\.user\.checkpasswd|checkpasswd",
    "crypt_call": r"crypt[[:space:]]*\(",
    "shadow_reference": r"/etc/shadow|getsp|getpwnam|pam_authenticate",
    "literal_admin": r"['\"]admin['\"]",
    "literal_root": r"['\"]root['\"]",
    "admin_equality": r"(==|~=)[[:space:]]*['\"]admin['\"]|['\"]admin['\"][[:space:]]*(==|~=)",
    "root_equality": r"(==|~=)[[:space:]]*['\"]root['\"]|['\"]root['\"][[:space:]]*(==|~=)",
    "admin_root_same_expression": r"admin[^\n]{0,120}root|root[^\n]{0,120}admin",
    "authenticator_symbol": r"authenticator",
    "sauth_symbol": r"sauth|session",
    "uci_password_lookup": r"uci[^\n]{0,120}(password|passwd|pwd)|(password|passwd|pwd)[^\n]{0,120}uci",
}
_INTERESTING_CALL_TOKEN = re.compile(
    r"auth|login|pass|pwd|user|account|session|token|crypt|encrypt|decrypt|config|uci|query|exec|system|validate|check|verify|permit|permission",
    re.IGNORECASE,
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]*$")
_SAFE_MODULE = re.compile(r"^[A-Za-z0-9_.+:-]+$")


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _command() -> str:
    files = " ".join("'" + p + "'" for p in _TARGETS)
    pieces = ["for f in " + files + "; do [ -f \"$f\" ] || continue; printf 'FILE\\t%s\\n' \"$f\";"]
    for name, pattern in _PATTERNS.items():
        safe = pattern.replace("'", "'\\''")
        pieces.append(
            "if grep -a -E -q '" + safe + "' \"$f\" 2>/dev/null; "
            "then printf 'FACT\\t%s\\t" + name + "\\n' \"$f\"; fi;"
        )
    pieces.append("done")
    return " ".join(pieces)


def _parse(stdout: str) -> dict[str, dict[str, bool]]:
    result: dict[str, dict[str, bool]] = {}
    for path in _TARGETS:
        result[path] = {name: False for name in _PATTERNS}
    for raw in stdout.splitlines():
        parts = raw.rstrip().split("\t")
        if len(parts) == 3 and parts[0] == "FACT" and parts[1] in result and parts[2] in _PATTERNS:
            result[parts[1]][parts[2]] = True
    return result


def _noauth_structure(source: str) -> dict:
    # Source is process-private. Only safe identifiers/module names and a digest leave this function.
    modules = set()
    aliases: dict[str, str] = {}
    for match in re.finditer(r"require\s*(?:\(\s*)?['\"]([A-Za-z0-9_.+:-]+)['\"]", source):
        module = match.group(1)
        if _SAFE_MODULE.fullmatch(module):
            modules.add(module)
    for match in re.finditer(
        r"(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*require\s*(?:\(\s*)?['\"]([A-Za-z0-9_.+:-]+)['\"]",
        source,
    ):
        alias, module = match.group(1), match.group(2)
        if _SAFE_IDENTIFIER.fullmatch(alias) and _SAFE_MODULE.fullmatch(module):
            aliases[alias] = module

    functions = set()
    for pattern in (
        r"function\s+([A-Za-z_][A-Za-z0-9_.:]*)\s*\(",
        r"(?:local\s+)?([A-Za-z_][A-Za-z0-9_.:]*)\s*=\s*function\s*\(",
    ):
        for match in re.finditer(pattern, source):
            name = match.group(1)
            if _SAFE_IDENTIFIER.fullmatch(name):
                functions.add(name)

    calls = set()
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.:]*)\s*\(", source):
        name = match.group(1)
        if _SAFE_IDENTIFIER.fullmatch(name) and _INTERESTING_CALL_TOKEN.search(name):
            calls.add(name)

    safe_literal_flags = {
        token: bool(re.search(r"['\"]" + re.escape(token) + r"['\"]", source))
        for token in (
            "username", "password", "passwd", "pwd", "admin", "root",
            "auth", "login", "api", "encry", "isCheckReadAgreement",
        )
    }
    return {
        "source_sha256": hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest(),
        "source_bytes": len(source.encode("utf-8", errors="ignore")),
        "require_modules": sorted(modules),
        "require_aliases": dict(sorted(aliases.items())),
        "function_names": sorted(functions),
        "interesting_call_identifiers": sorted(calls),
        "safe_literal_flags": safe_literal_flags,
        "source_lines_emitted": False,
        "arbitrary_string_literals_emitted": False,
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
        cp = await adapter.execute_shell(_command(), timeout=args.timeout, retries=1)
        if cp.exit_status not in (0, 1):
            raise RuntimeError(f"AUTH_SYMBOL_PROBE_EXIT:{cp.exit_status}")
        facts = _parse(cp.stdout or "")

        noauth_cp = await adapter.execute_shell(
            "cat '" + _NOAUTH + "' 2>/dev/null",
            timeout=args.timeout,
            retries=1,
        )
        if noauth_cp.exit_status != 0 or not (noauth_cp.stdout or ""):
            raise RuntimeError("NOAUTH_SOURCE_READ_FAILED")
        noauth_structure = _noauth_structure(noauth_cp.stdout or "")

        accounts_cp = await adapter.execute_shell(
            "cut -d: -f1 /etc/passwd 2>/dev/null | head -n 200",
            timeout=args.timeout,
            retries=1,
        )
        accounts = {line.strip() for line in (accounts_cp.stdout or "").splitlines() if line.strip()}
    finally:
        await adapter.disconnect()

    aggregate = {name: any(row[name] for row in facts.values()) for name in _PATTERNS}
    source_bound_system_auth = bool(
        aggregate["request_username"]
        and aggregate["request_password"]
        and (aggregate["sys_checkpasswd"] or aggregate["crypt_call"] or aggregate["shadow_reference"])
    )
    return {
        "schema": "dut-web-auth-symbol-probe-v2",
        "read_only": True,
        "mutation_executed": False,
        "secret_values_emitted": False,
        "secret_values_persisted": False,
        "source_lines_emitted": False,
        "files": facts,
        "aggregate": aggregate,
        "noauth_structure": noauth_structure,
        "system_accounts": {
            "root_present": "root" in accounts,
            "admin_present": "admin" in accounts,
            "account_count": len(accounts),
            "password_hashes_read": False,
        },
        "derived": {
            "request_credentials_feed_system_auth": source_bound_system_auth,
            "fixed_admin_mapping_proven": bool(aggregate["admin_equality"] and aggregate["admin_root_same_expression"]),
            "fixed_root_mapping_proven": bool(aggregate["root_equality"]),
            "uci_password_backend_proven": bool(aggregate["uci_password_lookup"]),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only structural WEB auth source probe")
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
    structure = payload["noauth_structure"]
    print(json.dumps({
        "DUT_WEB_AUTH_SYMBOL_PROBE": "PASS",
        "request_system_auth": payload["derived"]["request_credentials_feed_system_auth"],
        "fixed_admin_mapping": payload["derived"]["fixed_admin_mapping_proven"],
        "fixed_root_mapping": payload["derived"]["fixed_root_mapping_proven"],
        "uci_password_backend": payload["derived"]["uci_password_backend_proven"],
        "require_module_count": len(structure["require_modules"]),
        "interesting_call_count": len(structure["interesting_call_identifiers"]),
        "mutation": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

_NOAUTH = "/usr/lib/lua/luci/modules/noauth.lua"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]*$")
_SAFE_FIELD_NAMES = {
    "username", "user", "password", "passwd", "pwd", "encry", "time", "timestamp",
    "isCheckReadAgreement", "auth", "data", "body", "sid", "stok",
}
_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
    "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return", "then",
    "true", "until", "while",
}


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _login_region(source: str) -> str:
    match = re.search(r"(?m)^\s*(?:local\s+)?function\s+login\s*\([^\n]*\)", source)
    if not match:
        match = re.search(r"(?m)^\s*login\s*=\s*function\s*\([^\n]*\)", source)
    if not match:
        raise RuntimeError("LOGIN_FUNCTION_NOT_FOUND")
    tail = source[match.start():]
    next_fn = re.search(
        r"(?m)^\s*(?:local\s+)?function\s+(?!login\b)[A-Za-z_][A-Za-z0-9_.:]*\s*\(",
        tail[1:],
    )
    if next_fn:
        return tail[: 1 + next_fn.start()]
    return tail


def _calls(text: str) -> list[str]:
    found = set()
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.:]*)\s*\(", text):
        value = match.group(1)
        if _SAFE_IDENTIFIER.fullmatch(value) and value not in _KEYWORDS:
            found.add(value)
    return sorted(found)[:200]


def _identifiers(text: str) -> list[str]:
    found = []
    for value in re.findall(r"[A-Za-z_][A-Za-z0-9_.:]*", text):
        if value in _KEYWORDS or not _SAFE_IDENTIFIER.fullmatch(value):
            continue
        if value not in found:
            found.append(value)
    return found[:100]


def _safe_field_flags(text: str) -> list[str]:
    return sorted(
        field for field in _SAFE_FIELD_NAMES
        if re.search(r"['\"]" + re.escape(field) + r"['\"]", text)
    )


def _rhs_kind(rhs: str) -> str:
    stripped = rhs.strip()
    if re.fullmatch(r"(?:true|false|nil)", stripped):
        return "primitive"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", stripped):
        return "number"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:]*", stripped):
        return "identifier"
    if re.match(r"[A-Za-z_][A-Za-z0-9_.:]*\s*\(", stripped):
        return "call"
    if re.fullmatch(r"['\"].*['\"]", stripped):
        return "string_redacted"
    return "expression"


def _assignments(region: str) -> list[dict]:
    rows = []
    pattern = re.compile(
        r"(?m)^\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\r\n;]+)"
    )
    for match in pattern.finditer(region):
        lhs, rhs = match.group(1), match.group(2)
        rows.append({
            "lhs": lhs,
            "rhs_kind": _rhs_kind(rhs),
            "call_identifiers": _calls(rhs),
            "identifier_tokens": _identifiers(rhs),
            "safe_field_flags": _safe_field_flags(rhs),
            "contains_table_access": bool(re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*[.[]", rhs)),
            "rhs_emitted": False,
        })
    return rows[:200]


def _formvalue_fields(region: str) -> list[dict]:
    rows = []
    for match in re.finditer(
        r"([A-Za-z_][A-Za-z0-9_.:]*)\.formvalue\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
        region,
    ):
        receiver, field = match.group(1), match.group(2)
        rows.append({
            "receiver": receiver if _SAFE_IDENTIFIER.fullmatch(receiver) else "<redacted>",
            "field": field if field in _SAFE_FIELD_NAMES else "<other-field>",
        })
    return rows[:50]


def _comparisons(region: str) -> list[dict]:
    rows = []
    comparison = re.compile(
        r"([A-Za-z_][A-Za-z0-9_.:]*)\s*(==|~=)\s*([A-Za-z_][A-Za-z0-9_.:]*)"
    )
    for match in comparison.finditer(region):
        left, op, right = match.groups()
        if left in _KEYWORDS or right in _KEYWORDS:
            continue
        rows.append({"left": left, "operator": op, "right": right})
    return rows[:100]


def _conditions(region: str) -> list[dict]:
    rows = []
    for match in re.finditer(r"(?m)^\s*(?:if|elseif)\s+(.+?)\s+then\s*$", region):
        cond = match.group(1)
        rows.append({
            "call_identifiers": _calls(cond),
            "identifier_tokens": _identifiers(cond),
            "safe_field_flags": _safe_field_flags(cond),
            "condition_emitted": False,
        })
    return rows[:100]


def _member_refs(region: str) -> list[str]:
    found = set()
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*\.(?:pwd|passwd|password|username|user|encry|data))\b", region):
        value = match.group(1)
        if _SAFE_IDENTIFIER.fullmatch(value):
            found.add(value)
    return sorted(found)[:100]


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
        cp = await adapter.execute_shell("cat '" + _NOAUTH + "' 2>/dev/null", timeout=args.timeout, retries=1)
        if cp.exit_status != 0 or not (cp.stdout or ""):
            raise RuntimeError("NOAUTH_SOURCE_READ_FAILED")
        source = cp.stdout or ""
    finally:
        await adapter.disconnect()

    region = _login_region(source)
    return {
        "schema": "dut-web-login-flow-source-v1",
        "read_only": True,
        "mutation_executed": False,
        "secret_values_emitted": False,
        "secret_values_persisted": False,
        "source_sha256": hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest(),
        "login_region_sha256": hashlib.sha256(region.encode("utf-8", errors="ignore")).hexdigest(),
        "login_region_bytes": len(region.encode("utf-8", errors="ignore")),
        "call_identifiers": _calls(region),
        "assignments": _assignments(region),
        "formvalue_fields": _formvalue_fields(region),
        "identifier_comparisons": _comparisons(region),
        "conditions": _conditions(region),
        "credential_member_refs": _member_refs(region),
        "safe_field_flags": _safe_field_flags(region),
        "source_lines_emitted": False,
        "rhs_values_emitted": False,
        "arbitrary_string_literals_emitted": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Secret-safe structural data-flow probe for DUT WEB login()")
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
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "DUT_WEB_LOGIN_FLOW_PROBE": "PASS",
        "assignment_count": len(payload["assignments"]),
        "call_count": len(payload["call_identifiers"]),
        "comparison_count": len(payload["identifier_comparisons"]),
        "credential_member_ref_count": len(payload["credential_member_refs"]),
        "formvalue_field_count": len(payload["formvalue_fields"]),
        "mutation": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

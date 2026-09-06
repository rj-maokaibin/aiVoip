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
_KEYWORDS = {"and","break","do","else","elseif","end","false","for","function","if","in","local","nil","not","or","repeat","return","then","true","until","while"}
_TRACKED = {"authres", "username", "password", "encry", "limit", "log_opt", "_pwd", "checkStat", "params.pwd", "params.password", "params.username"}


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _login_region(source: str) -> str:
    m = re.search(r"(?m)^\s*(?:local\s+)?function\s+login\s*\([^\n]*\)", source)
    if not m:
        m = re.search(r"(?m)^\s*login\s*=\s*function\s*\([^\n]*\)", source)
    if not m:
        raise RuntimeError("LOGIN_FUNCTION_NOT_FOUND")
    tail = source[m.start():]
    n = re.search(r"(?m)^\s*(?:local\s+)?function\s+(?!login\b)[A-Za-z_][A-Za-z0-9_.:]*\s*\(", tail[1:])
    return tail[:1+n.start()] if n else tail


def _ids(text: str) -> list[str]:
    out=[]
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.:]*", text):
        if token in _KEYWORDS or not _SAFE_IDENTIFIER.fullmatch(token):
            continue
        if token not in out:
            out.append(token)
    return out[:100]


def _calls(text: str) -> list[str]:
    out=[]
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_.:]*)\s*\(", text):
        name=m.group(1)
        if name not in _KEYWORDS and _SAFE_IDENTIFIER.fullmatch(name) and name not in out:
            out.append(name)
    return out[:50]


def _rhs_kind(text: str) -> str:
    s=text.strip()
    if re.fullmatch(r"(?:true|false|nil)", s): return "primitive"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s): return "number"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:]*", s): return "identifier"
    if re.match(r"[A-Za-z_][A-Za-z0-9_.:]*\s*\(", s): return "call"
    if re.fullmatch(r"['\"].*['\"]", s): return "string_redacted"
    return "expression"


def _interesting_assignments(region: str) -> list[dict]:
    rows=[]
    pat=re.compile(r"(?m)^\s*(?:local\s+)?((?:[A-Za-z_][A-Za-z0-9_]*\s*,\s*)*[A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\r\n;]+)")
    for m in pat.finditer(region):
        lhs=[x.strip() for x in m.group(1).split(',')]
        rhs=m.group(2)
        ids=_ids(rhs)
        if "authres" not in lhs and not (set(ids) & _TRACKED):
            continue
        rows.append({
            "lhs": lhs,
            "rhs_kind": _rhs_kind(rhs),
            "call_identifiers": _calls(rhs),
            "identifier_tokens": ids,
            "contains_authres_rhs": "authres" in ids,
            "rhs_emitted": False,
        })
    return rows[:100]


def _authres_references(region: str) -> list[dict]:
    rows=[]
    for line in region.splitlines():
        if not re.search(r"\bauthres\b", line):
            continue
        stripped=line.strip()
        kind="reference"
        if re.match(r"(?:local\s+)?[^=]+\bauthres\b[^=]*=", stripped): kind="assignment"
        elif re.match(r"(?:if|elseif)\b", stripped): kind="condition"
        elif stripped.startswith("return "): kind="return"
        rows.append({
            "kind": kind,
            "call_identifiers": _calls(stripped),
            "identifier_tokens": _ids(stripped),
            "operators": sorted(set(re.findall(r"==|~=|<=|>=|<|>", stripped))),
            "source_line_emitted": False,
        })
    return rows[:50]


def _credential_calls(region: str) -> list[dict]:
    rows=[]
    for line in region.splitlines():
        ids=_ids(line)
        tracked=sorted(set(ids) & _TRACKED)
        calls=_calls(line)
        if not tracked or not calls:
            continue
        rows.append({
            "call_identifiers": calls,
            "tracked_identifier_tokens": tracked,
            "all_identifier_tokens": ids,
            "source_line_emitted": False,
        })
    return rows[:100]


async def _probe(args) -> dict:
    _env_password(args.password_env)
    spec=GateDeviceSpec(device_id=args.device_id, model=args.model, host=args.host, port=args.port, username=args.username, platform_id=args.platform_id)
    adapter=build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    try:
        cp=await adapter.execute_shell("cat '"+_NOAUTH+"' 2>/dev/null", timeout=args.timeout, retries=1)
        if cp.exit_status != 0 or not (cp.stdout or ""):
            raise RuntimeError("NOAUTH_SOURCE_READ_FAILED")
        source=cp.stdout or ""
    finally:
        await adapter.disconnect()
    region=_login_region(source)
    return {
        "schema":"dut-web-authres-flow-source-v1",
        "read_only":True,
        "mutation_executed":False,
        "secret_values_emitted":False,
        "secret_values_persisted":False,
        "source_sha256":hashlib.sha256(source.encode("utf-8",errors="ignore")).hexdigest(),
        "login_region_sha256":hashlib.sha256(region.encode("utf-8",errors="ignore")).hexdigest(),
        "interesting_assignments":_interesting_assignments(region),
        "authres_references":_authres_references(region),
        "credential_calls":_credential_calls(region),
        "source_lines_emitted":False,
        "rhs_values_emitted":False,
    }


def main() -> int:
    p=argparse.ArgumentParser(description="Secret-safe authres verifier binding for current DUT WEB login")
    p.add_argument("--device-id",required=True); p.add_argument("--model",required=True); p.add_argument("--host",required=True)
    p.add_argument("--port",type=int,default=22); p.add_argument("--username",default="root"); p.add_argument("--platform-id",default=None)
    p.add_argument("--password-env",required=True); p.add_argument("--timeout",type=float,default=30.0); p.add_argument("--output",required=True)
    args=p.parse_args(); payload=asyncio.run(_probe(args)); out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"DUT_WEB_AUTHRES_FLOW_PROBE":"PASS","assignment_count":len(payload["interesting_assignments"]),"authres_reference_count":len(payload["authres_references"]),"credential_call_count":len(payload["credential_calls"]),"mutation":False,"secret_values_emitted":False},sort_keys=True))
    return 0

if __name__=="__main__": raise SystemExit(main())

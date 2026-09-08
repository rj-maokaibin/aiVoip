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

_TOOL_PATHS=("/usr/lib/lua/luci/utils/tool.lua","/rom/usr/lib/lua/luci/utils/tool.lua")
_SAFE_ID=re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]*$")
_KEYWORDS={"and","break","do","else","elseif","end","false","for","function","if","in","local","nil","not","or","repeat","return","then","true","until","while"}
_SAFE_KEYS={"admin","root","password","passwd","pwd","username","user","auth","login","webpw","module","capacity.webpw.module","capacity.web","flash_keep"}


def _env_password(ref:str)->None:
    if not ref.startswith("ENV:") or not os.getenv(ref.removeprefix("ENV:")):
        raise RuntimeError("SSH_PASSWORD_ENV_REQUIRED")


def _region(source:str)->str:
    m=re.search(r"(?m)^\s*(?:local\s+)?function\s+(?:[A-Za-z_][A-Za-z0-9_.:]*[.:])?checkPasswd\s*\([^\n]*\)",source)
    if not m:
        m=re.search(r"(?m)^\s*(?:[A-Za-z_][A-Za-z0-9_.:]*[.:])?checkPasswd\s*=\s*function\s*\([^\n]*\)",source)
    if not m: raise RuntimeError("CHECKPASSWD_FUNCTION_NOT_FOUND")
    tail=source[m.start():]
    n=re.search(r"(?m)^\s*(?:local\s+)?function\s+[A-Za-z_][A-Za-z0-9_.:]*\s*\(",tail[1:])
    return tail[:1+n.start()] if n else tail


def _ids(text:str)->list[str]:
    out=[]
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_.:]*",text):
        if t in _KEYWORDS or not _SAFE_ID.fullmatch(t): continue
        if t not in out: out.append(t)
    return out[:120]


def _calls(text:str)->list[str]:
    out=[]
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_.:]*)\s*\(",text):
        n=m.group(1)
        if n not in _KEYWORDS and _SAFE_ID.fullmatch(n) and n not in out: out.append(n)
    return out[:100]


def _kind(rhs:str)->str:
    s=rhs.strip()
    if re.fullmatch(r"(?:true|false|nil)",s): return "primitive"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?",s): return "number"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:]*",s): return "identifier"
    if re.match(r"[A-Za-z_][A-Za-z0-9_.:]*\s*\(",s): return "call"
    if re.fullmatch(r"['\"].*['\"]",s): return "string_redacted"
    return "expression"


def _safe_literal_flags(text:str)->list[str]:
    return sorted(k for k in _SAFE_KEYS if re.search(r"['\"]"+re.escape(k)+r"['\"]",text))


def _assignments(region:str)->list[dict]:
    out=[]
    pat=re.compile(r"(?m)^\s*(?:local\s+)?((?:[A-Za-z_][A-Za-z0-9_]*\s*,\s*)*[A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\r\n;]+)")
    for m in pat.finditer(region):
        rhs=m.group(2)
        out.append({"lhs":[x.strip() for x in m.group(1).split(',')],"rhs_kind":_kind(rhs),"call_identifiers":_calls(rhs),"identifier_tokens":_ids(rhs),"safe_literal_flags":_safe_literal_flags(rhs),"rhs_emitted":False})
    return out[:150]


def _conditions(region:str)->list[dict]:
    out=[]
    for m in re.finditer(r"(?m)^\s*(?:if|elseif)\s+(.+?)\s+then\s*$",region):
        c=m.group(1)
        out.append({"call_identifiers":_calls(c),"identifier_tokens":_ids(c),"safe_literal_flags":_safe_literal_flags(c),"operators":sorted(set(re.findall(r"==|~=|<=|>=|<|>",c))),"condition_emitted":False})
    return out[:100]


def _returns(region:str)->list[dict]:
    out=[]
    for m in re.finditer(r"(?m)^\s*return\s+([^\r\n]+)",region):
        r=m.group(1)
        out.append({"call_identifiers":_calls(r),"identifier_tokens":_ids(r),"safe_literal_flags":_safe_literal_flags(r),"return_value_emitted":False})
    return out[:50]


def _uci_calls(region:str)->list[dict]:
    out=[]
    for line in region.splitlines():
        calls=[c for c in _calls(line) if "uci" in c.lower() or c.endswith(":get") or c.endswith(":get_first")]
        if not calls: continue
        dotted=[]
        for s in re.findall(r"['\"]([A-Za-z0-9_@.\[\]-]{2,120})['\"]",line):
            if "." in s and not re.search(r"pass|secret|token",s,re.I): dotted.append(s)
            elif s in {"capacity","webpw","module","web","auth","user","admin"}: dotted.append(s)
        out.append({"call_identifiers":calls,"identifier_tokens":_ids(line),"safe_key_literals":sorted(set(dotted)),"source_line_emitted":False})
    return out[:50]


async def _probe(args)->dict:
    _env_password(args.password_env)
    spec=GateDeviceSpec(device_id=args.device_id,model=args.model,host=args.host,port=args.port,username=args.username,platform_id=args.platform_id)
    ad=build_asyncssh_adapter(spec,password_env=args.password_env); await ad.connect()
    try:
        source=""; path=""
        for candidate in _TOOL_PATHS:
            cp=await ad.execute_shell("cat '"+candidate+"' 2>/dev/null",timeout=args.timeout,retries=1)
            if cp.exit_status==0 and (cp.stdout or ""):
                source=cp.stdout or ""; path=candidate; break
        if not source: raise RuntimeError("CHECKPASSWD_SOURCE_NOT_FOUND")
    finally: await ad.disconnect()
    region=_region(source)
    return {"schema":"dut-web-checkpasswd-flow-v1","read_only":True,"mutation_executed":False,"secret_values_emitted":False,"secret_values_persisted":False,"tool_source_path":path,"source_sha256":hashlib.sha256(source.encode()).hexdigest(),"function_sha256":hashlib.sha256(region.encode()).hexdigest(),"function_bytes":len(region.encode()),"call_identifiers":_calls(region),"identifier_tokens":_ids(region),"safe_literal_flags":_safe_literal_flags(region),"assignments":_assignments(region),"conditions":_conditions(region),"returns":_returns(region),"uci_calls":_uci_calls(region),"source_lines_emitted":False,"arbitrary_string_literals_emitted":False}


def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--device-id",required=True); p.add_argument("--model",required=True); p.add_argument("--host",required=True); p.add_argument("--port",type=int,default=22); p.add_argument("--username",default="root"); p.add_argument("--platform-id",default=None); p.add_argument("--password-env",required=True); p.add_argument("--timeout",type=float,default=30.0); p.add_argument("--output",required=True)
    a=p.parse_args(); payload=asyncio.run(_probe(a)); out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"DUT_WEB_CHECKPASSWD_FLOW_PROBE":"PASS","call_count":len(payload["call_identifiers"]),"assignment_count":len(payload["assignments"]),"condition_count":len(payload["conditions"]),"uci_call_count":len(payload["uci_calls"]),"mutation":False,"secret_values_emitted":False},sort_keys=True)); return 0

if __name__=="__main__": raise SystemExit(main())

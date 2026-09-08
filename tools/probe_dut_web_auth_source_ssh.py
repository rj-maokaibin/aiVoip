#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec

_NEEDLES = (
    "/cgi-bin/luci/api/auth",
    "isCheckReadAgreement",
    "encry",
    "pwd",
    "encrypt",
    "CryptoJS",
    "JSEncrypt",
    "AES",
    "RSA",
    "publicKey",
    "setPublicKey",
    "PKCS",
)
# Program/static source roots only. Deliberately exclude /etc, /tmp, /overlay,
# /root and other configuration/state locations which may contain credentials.
_SAFE_ROOTS = (
    "/www",
    "/rom/www",
    "/usr/share/luci",
    "/usr/lib/lua/luci",
    "/usr/lib/lua",
)


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _grep_pattern() -> str:
    return "|".join(re.escape(item) for item in _NEEDLES)


def _read_only_command() -> str:
    # Read program/static files only. -a is intentional so minified/bundled assets
    # and executable wrappers can still expose source-bound string evidence. Bound
    # output width/count so no large binary/source blob can enter the artifact.
    roots = " ".join(_SAFE_ROOTS)
    pattern = _grep_pattern().replace("'", "'\\''")
    return (
        "for root in " + roots + "; do "
        "[ -e \"$root\" ] || continue; "
        f"grep -R -a -n -E '{pattern}' \"$root\" 2>/dev/null; "
        "done | head -n 800 | cut -c1-5000"
    )


def _layout_command() -> str:
    # Discovery metadata only: resolve candidate roots and list a bounded set of
    # JS/HTML/CGI asset paths. Never print file content here.
    roots = " ".join(_SAFE_ROOTS)
    return (
        "for root in " + roots + "; do "
        "[ -e \"$root\" ] || continue; "
        "printf 'ROOT\\t%s\\t%s\\n' \"$root\" \"$(readlink -f \"$root\" 2>/dev/null || true)\"; "
        "find -L \"$root\" -maxdepth 5 -type f "
        "\\( -name '*.js' -o -name '*.mjs' -o -name '*.html' -o -name '*.htm' "
        "-o -name '*.lua' -o -name 'luci' -o -path '*/cgi-bin/*' \\) "
        "-printf 'FILE\\t%p\\n' 2>/dev/null | head -n 400; "
        "done"
    )


def _parse(stdout: str) -> list[dict]:
    results: list[dict] = []
    for raw in stdout.splitlines():
        match = re.match(r"^([^:]+):(\d+):(.*)$", raw)
        if not match:
            continue
        path, line_no, text = match.groups()
        if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in _SAFE_ROOTS):
            continue
        matched = [needle for needle in _NEEDLES if needle.lower() in text.lower()]
        if not matched:
            continue
        results.append({
            "path": path,
            "line": int(line_no),
            "needles": matched,
            "excerpt": text,
        })
    return results


def _parse_layout(stdout: str) -> dict:
    roots: list[dict] = []
    files: list[str] = []
    for raw in stdout.splitlines():
        parts = raw.rstrip().split("\t", 2)
        if not parts:
            continue
        if parts[0] == "ROOT" and len(parts) == 3:
            logical, resolved = parts[1], parts[2]
            if logical in _SAFE_ROOTS:
                roots.append({"logical": logical, "resolved": resolved})
        elif parts[0] == "FILE" and len(parts) >= 2:
            path = parts[1]
            if any(path == root or path.startswith(root.rstrip("/") + "/") for root in _SAFE_ROOTS):
                files.append(path)
    return {"roots": roots, "candidate_files": files[:400]}


async def probe(args) -> dict:
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
        result = await adapter.execute_shell(_read_only_command(), timeout=args.timeout, retries=1)
        layout_result = await adapter.execute_shell(_layout_command(), timeout=args.timeout, retries=1)
    finally:
        await adapter.disconnect()
    if result.exit_status not in (0, 1):
        raise RuntimeError(f"READ_ONLY_SOURCE_GREP_EXIT:{result.exit_status}")
    if layout_result.exit_status not in (0, 1):
        raise RuntimeError(f"READ_ONLY_SOURCE_LAYOUT_EXIT:{layout_result.exit_status}")
    matches = _parse(result.stdout or "")
    layout = _parse_layout(layout_result.stdout or "")
    return {
        "schema": "dut-web-auth-static-source-probe-v2",
        "device_id": args.device_id,
        "model": args.model,
        "platform_id": args.platform_id,
        "read_only": True,
        "mutation_executed": False,
        "credential_value_persisted": False,
        "config_state_roots_scanned": False,
        "roots": list(_SAFE_ROOTS),
        "needles": list(_NEEDLES),
        "layout": layout,
        "match_count": len(matches),
        "matches": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DUT filesystem WEB auth source probe")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = asyncio.run(probe(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "DUT_WEB_AUTH_STATIC_SOURCE_PROBE=PASS "
        f"matches={payload['match_count']} mutation=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

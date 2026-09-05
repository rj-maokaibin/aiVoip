#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec


_HANDLER_NEEDLES = (
    "/cgi-bin/luci/api/auth",
    "isCheckReadAgreement",
)
_BACKEND_MARKERS = (
    "admin",
    "root",
    "/etc/shadow",
    "checkpasswd",
    "crypt",
    "getpwnam",
    "pam_authenticate",
    "luci.sys.user.checkpasswd",
    "sys.user.checkpasswd",
    "username",
    "password",
    "auth",
    "login",
)
_SAFE_ROOTS = ("/www", "/rom/www", "/usr/share", "/usr/lib", "/lib")
_SAFE_PATH = re.compile(r"^/(?:www|rom/www|usr/share|usr/lib|lib)(?:/|$)")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.+:-]+$")


class _LoginSurfaceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str]] = []
        self.forms: list[str] = []
        self.scripts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        data = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag.lower() == "input":
            # Never record input values. Names and types are enough to bind the UI contract.
            self.inputs.append({"name": data.get("name", ""), "type": data.get("type", "")})
        elif tag.lower() == "form":
            if data.get("action"):
                self.forms.append(data["action"])
        elif tag.lower() == "script":
            if data.get("src"):
                self.scripts.append(data["src"])
        elif tag.lower() == "link":
            if data.get("href"):
                self.links.append(data["href"])


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _same_origin_paths(base_url: str, values: list[str]) -> list[str]:
    base = urlparse(base_url)
    result: list[str] = []
    for value in values:
        try:
            resolved = urlparse(urljoin(base_url, value))
        except Exception:
            continue
        if (resolved.scheme, resolved.netloc) != (base.scheme, base.netloc):
            continue
        path = resolved.path or "/"
        if resolved.query:
            path += "?" + resolved.query
        result.append(path)
    return sorted(set(result))[:100]


def _handler_path_command() -> str:
    roots = " ".join(_SAFE_ROOTS)
    pattern = "|".join(re.escape(item) for item in _HANDLER_NEEDLES).replace("'", "'\\''")
    return (
        "for root in " + roots + "; do "
        "[ -e \"$root\" ] || continue; "
        f"grep -R -l -a -E '{pattern}' \"$root\" 2>/dev/null; "
        "done | head -n 100"
    )


def _package_list_command() -> str:
    # Package/file metadata only; no configuration or credential state is read.
    return (
        "opkg list-installed 2>/dev/null | awk '{print $1}' | "
        "grep -E '(^luci($|-)|eweb|web)' | head -n 100"
    )


def _package_files_command(packages: list[str]) -> str:
    safe = [pkg for pkg in packages if _PACKAGE_NAME.fullmatch(pkg)]
    if not safe:
        return "true"
    quoted = " ".join("'" + pkg.replace("'", "'\\''") + "'" for pkg in safe[:50])
    return (
        "for pkg in " + quoted + "; do "
        "printf 'PKG\\t%s\\n' \"$pkg\"; "
        "opkg files \"$pkg\" 2>/dev/null | grep '^/' | head -n 1000 | sed 's/^/FILE\\t/'; "
        "done"
    )


def _parse_package_files(stdout: str) -> tuple[list[str], list[str]]:
    packages: list[str] = []
    files: list[str] = []
    current = ""
    for raw in stdout.splitlines():
        parts = raw.rstrip().split("\t", 1)
        if len(parts) != 2:
            continue
        if parts[0] == "PKG" and _PACKAGE_NAME.fullmatch(parts[1]):
            current = parts[1]
            packages.append(current)
        elif parts[0] == "FILE" and current and _SAFE_PATH.match(parts[1]):
            files.append(parts[1])
    return sorted(set(packages))[:100], sorted(set(files))[:3000]


def _files_matching_needles_command(paths: list[str]) -> str:
    safe = [path for path in paths if _SAFE_PATH.match(path)]
    if not safe:
        return "true"
    quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in safe[:1000])
    needles = " ".join("'" + n.replace("'", "'\\''") + "'" for n in _HANDLER_NEEDLES)
    return (
        "for f in " + quoted + "; do "
        "[ -f \"$f\" ] || continue; "
        "for needle in " + needles + "; do "
        "if grep -a -q -F -- \"$needle\" \"$f\" 2>/dev/null; then printf '%s\\n' \"$f\"; break; fi; "
        "done; done | head -n 100"
    )


def _marker_command(paths: list[str]) -> str:
    safe = [path for path in paths if _SAFE_PATH.match(path)]
    if not safe:
        return "true"
    quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in safe[:100])
    markers = " ".join("'" + marker.replace("'", "'\\''") + "'" for marker in _BACKEND_MARKERS)
    return (
        "for f in " + quoted + "; do "
        "[ -f \"$f\" ] || continue; "
        "printf 'FILE\\t%s' \"$f\"; "
        "for marker in " + markers + "; do "
        "if grep -a -q -F -- \"$marker\" \"$f\" 2>/dev/null; then printf '\\t%s' \"$marker\"; fi; "
        "done; printf '\\n'; done"
    )


def _parse_markers(stdout: str) -> list[dict]:
    rows: list[dict] = []
    allowed = set(_BACKEND_MARKERS)
    for raw in stdout.splitlines():
        parts = raw.rstrip().split("\t")
        if len(parts) < 2 or parts[0] != "FILE":
            continue
        path = parts[1]
        if not _SAFE_PATH.match(path):
            continue
        rows.append({"path": path, "markers": [m for m in parts[2:] if m in allowed]})
    return rows[:100]


async def _probe(args) -> dict:
    _env_password(args.password_env)

    async with httpx.AsyncClient(verify=not args.insecure_tls, follow_redirects=True, timeout=10.0) as client:
        response = await client.get(args.base_url.rstrip("/") + "/")
        response.raise_for_status()
        html = response.text[:500_000]
        parser = _LoginSurfaceParser()
        parser.feed(html)
        public_surface = {
            "status_code": response.status_code,
            "final_path": urlparse(str(response.url)).path or "/",
            "input_fields": parser.inputs[:100],
            "form_paths": _same_origin_paths(args.base_url, parser.forms),
            "script_paths": _same_origin_paths(args.base_url, parser.scripts),
            "link_paths": _same_origin_paths(args.base_url, parser.links),
            "literal_admin_present": bool(re.search(r"(?<![A-Za-z0-9_])admin(?![A-Za-z0-9_])", html, re.IGNORECASE)),
            "literal_root_present": bool(re.search(r"(?<![A-Za-z0-9_])root(?![A-Za-z0-9_])", html, re.IGNORECASE)),
            "credential_values_recorded": False,
        }

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
        paths_result = await adapter.execute_shell(_handler_path_command(), timeout=args.timeout, retries=1)
        if paths_result.exit_status not in (0, 1):
            raise RuntimeError(f"HANDLER_PATH_DISCOVERY_EXIT:{paths_result.exit_status}")
        generic_handler_paths = sorted({
            line.strip() for line in (paths_result.stdout or "").splitlines()
            if line.strip() and _SAFE_PATH.match(line.strip())
        })[:100]

        pkg_result = await adapter.execute_shell(_package_list_command(), timeout=args.timeout, retries=1)
        installed_packages = [
            line.strip() for line in (pkg_result.stdout or "").splitlines()
            if line.strip() and _PACKAGE_NAME.fullmatch(line.strip())
        ][:100]
        pkg_files_result = await adapter.execute_shell(
            _package_files_command(installed_packages), timeout=args.timeout, retries=1
        )
        package_names, package_files = _parse_package_files(pkg_files_result.stdout or "")
        package_handler_result = await adapter.execute_shell(
            _files_matching_needles_command(package_files), timeout=args.timeout, retries=1
        )
        package_handler_paths = sorted({
            line.strip() for line in (package_handler_result.stdout or "").splitlines()
            if line.strip() and _SAFE_PATH.match(line.strip())
        })[:100]

        # Even when the exact endpoint string is dynamically assembled, files whose
        # path is explicitly auth/login-related are valid source candidates.
        package_auth_named_paths = sorted({
            path for path in package_files
            if re.search(r"(?:^|[/_.-])(auth|login|session|dispatcher)(?:[/_.-]|$)", path, re.IGNORECASE)
        })[:100]
        handler_paths = sorted(set(generic_handler_paths + package_handler_paths + package_auth_named_paths))[:100]

        marker_result = await adapter.execute_shell(_marker_command(handler_paths), timeout=args.timeout, retries=1)
        if marker_result.exit_status not in (0, 1):
            raise RuntimeError(f"HANDLER_MARKER_DISCOVERY_EXIT:{marker_result.exit_status}")
        handler_markers = _parse_markers(marker_result.stdout or "")

        account_result = await adapter.execute_shell(
            "cut -d: -f1 /etc/passwd 2>/dev/null | head -n 200",
            timeout=args.timeout,
            retries=1,
        )
        accounts = {line.strip() for line in (account_result.stdout or "").splitlines() if line.strip()}
    finally:
        await adapter.disconnect()

    markers = {marker for row in handler_markers for marker in row["markers"]}
    return {
        "schema": "dut-web-login-backend-source-v2",
        "read_only": True,
        "mutation_executed": False,
        "secret_values_emitted": False,
        "secret_values_persisted": False,
        "public_login_surface": public_surface,
        "installed_web_packages": package_names,
        "installed_web_package_file_count": len(package_files),
        "package_handler_paths": package_handler_paths,
        "package_auth_named_paths": package_auth_named_paths,
        "generic_handler_paths": generic_handler_paths,
        "handler_paths": handler_paths,
        "handler_marker_summary": sorted(markers),
        "handler_files": handler_markers,
        "system_account_metadata": {
            "root_present": "root" in accounts,
            "admin_present": "admin" in accounts,
            "account_count": len(accounts),
            "password_hashes_read": False,
        },
        "source_binding": {
            "admin_ui_literal": public_surface["literal_admin_present"],
            "admin_handler_marker": "admin" in markers,
            "root_handler_marker": "root" in markers,
            "system_shadow_marker": bool(markers & {"/etc/shadow", "checkpasswd", "crypt", "getpwnam", "pam_authenticate", "luci.sys.user.checkpasswd", "sys.user.checkpasswd"}),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace current DUT WEB login backend without reading credential values")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--insecure-tls", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = asyncio.run(_probe(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "DUT_WEB_LOGIN_BACKEND_PROBE": "PASS",
        "installed_web_package_count": len(payload["installed_web_packages"]),
        "installed_web_package_file_count": payload["installed_web_package_file_count"],
        "handler_path_count": len(payload["handler_paths"]),
        "handler_marker_count": len(payload["handler_marker_summary"]),
        "admin_ui_literal": payload["source_binding"]["admin_ui_literal"],
        "admin_handler_marker": payload["source_binding"]["admin_handler_marker"],
        "system_shadow_marker": payload["source_binding"]["system_shadow_marker"],
        "mutation": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

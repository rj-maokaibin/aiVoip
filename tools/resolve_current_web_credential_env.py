#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from app.automation.adapters.web_auth.apf3260m import build_apf3260m_luci_auth_provider
from app.automation.adapters.web_auth.base import WebCredential
from app.infrastructure.transport.http import HttpApiTransport


@dataclass(frozen=True)
class _Candidate:
    username: str
    password: str = field(repr=False, compare=False)
    sources: tuple[str, ...] = ()


def _read_secret_from_mounted_container(path: Path) -> tuple[str, str]:
    """Read an already-mounted production secret without copying or logging it."""
    try:
        listed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", "unreadable"
    if listed.returncode != 0:
        return "", "unreadable"

    destination = str(path)
    for name in listed.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        try:
            inspected = subprocess.run(
                ["docker", "inspect", name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if inspected.returncode != 0:
            continue
        try:
            item = json.loads(inspected.stdout)[0]
        except Exception:
            continue
        labels = dict(((item.get("Config") or {}).get("Labels") or {}))
        if labels.get("com.docker.compose.project") != "aivoip":
            continue
        mounted = any(
            str(mount.get("Destination") or "") == destination
            for mount in (item.get("Mounts") or [])
        )
        if not mounted:
            continue
        try:
            completed = subprocess.run(
                ["docker", "exec", "-u", "0", name, "cat", destination],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout, "docker_mounted_secret"
    return "", "unreadable"


def _read_secret_text(path: Path) -> tuple[str, str]:
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8", errors="ignore"), "runner"
        except PermissionError:
            try:
                completed = subprocess.run(
                    ["sudo", "-n", "cat", str(path)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0 and completed.stdout:
                return completed.stdout, "sudo_pipe"
    return _read_secret_from_mounted_container(path)


def _parse_secret(secret_text: str) -> Any:
    if not secret_text:
        return {}
    try:
        return yaml.safe_load(secret_text) or {}
    except Exception:
        return {}


def _matching_secret_candidates_from_root(
    root: Any, *, allowed_hosts: dict[str, str]
) -> list[_Candidate]:
    found: list[_Candidate] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            lowered = {str(key).lower(): key for key in value}
            username_key = lowered.get("username")
            password_key = lowered.get("password")
            host_key = lowered.get("host") or lowered.get("ip")
            if username_key is not None and password_key is not None and host_key is not None:
                host = str(value.get(host_key) or "").strip()
                username = str(value.get(username_key) or "").strip()
                password = str(value.get(password_key) or "")
                binding = allowed_hosts.get(host)
                if binding and username and password:
                    source_path = path or "<root>"
                    found.append(
                        _Candidate(username, password, (f"{source_path}#host={binding}",))
                    )
            for key, child in value.items():
                key_text = str(key)
                walk(child, f"{path}.{key_text}" if path else key_text)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]" if path else f"[{index}]")

    walk(root)
    return found


def _safe_secret_schema_metadata(
    root: Any, *, device_host: str, web_host: str, limit: int = 100
) -> list[dict[str, Any]]:
    """Expose key-path/type/match metadata only; never expose scalar secret values."""
    rows: list[dict[str, Any]] = []
    interesting = {
        "username", "user", "admin", "account", "login",
        "password", "passwd", "pwd", "pass",
        "host", "ip", "url", "web", "luci", "http", "https",
    }

    def walk(value: Any, path: str = "") -> None:
        if len(rows) >= limit:
            return
        if isinstance(value, dict):
            lowered = {str(key).lower(): key for key in value}
            matched = sorted(set(lowered) & interesting)
            if matched:
                host_key = lowered.get("host") or lowered.get("ip")
                host = str(value.get(host_key) or "").strip() if host_key is not None else ""
                rows.append({
                    "path": path or "<root>",
                    "matching_key_names": matched,
                    "host_key_present": host_key is not None,
                    "host_matches_device": bool(host and host == device_host),
                    "host_matches_web_endpoint": bool(host and web_host and host == web_host),
                    "username_like_key_present": bool(
                        set(lowered) & {"username", "user", "admin", "account", "login"}
                    ),
                    "password_like_key_present": bool(
                        set(lowered) & {"password", "passwd", "pwd", "pass"}
                    ),
                    "value_types": {
                        str(key): type(value.get(original_key)).__name__
                        for key, original_key in sorted(lowered.items())
                        if key in interesting
                    },
                })
            for key, child in value.items():
                key_text = str(key)
                walk(child, f"{path}.{key_text}" if path else key_text)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]" if path else f"[{index}]")

    walk(root)
    return rows


def _dedupe(candidates: list[_Candidate]) -> list[_Candidate]:
    merged: dict[tuple[str, str], set[str]] = {}
    for item in candidates:
        key = (item.username, item.password)
        merged.setdefault(key, set()).update(item.sources)
    return [
        _Candidate(username, password, tuple(sorted(sources)))
        for (username, password), sources in merged.items()
    ]


async def _authenticate_candidate(
    *, base_url: str, candidate: _Candidate, insecure_tls: bool
) -> bool:
    client = httpx.AsyncClient(base_url=base_url, verify=not insecure_tls)
    transport = HttpApiTransport(base_url, client=client)
    provider = build_apf3260m_luci_auth_provider(
        timestamp_provider=lambda: str(int(time.time()))
    )
    try:
        await provider.authenticate(
            transport,
            WebCredential(username=candidate.username, password=candidate.password),
        )
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


async def _resolve(args) -> tuple[_Candidate | None, dict[str, Any]]:
    candidates: list[_Candidate] = []
    env_username = os.environ.get(args.username_env, "").strip()
    env_password = os.environ.get(args.password_env, "")
    if env_username and env_password:
        candidates.append(_Candidate(env_username, env_password, ("resolved_device_credential",)))

    web_host = (urlsplit(args.base_url).hostname or "").strip()
    allowed_hosts: dict[str, str] = {}
    if args.device_host:
        allowed_hosts[args.device_host] = "device_host"
    if web_host:
        allowed_hosts.setdefault(web_host, "web_endpoint_host")

    secret_text, metadata_mode = _read_secret_text(Path(args.secret_file))
    try:
        secret_root = _parse_secret(secret_text)
        secret_candidates = _matching_secret_candidates_from_root(
            secret_root, allowed_hosts=allowed_hosts
        )
        schema_metadata = _safe_secret_schema_metadata(
            secret_root, device_host=args.device_host, web_host=web_host
        )
        candidates.extend(secret_candidates)
    finally:
        secret_text = ""

    candidates = _dedupe(candidates)
    successes: list[_Candidate] = []
    for candidate in candidates:
        if await _authenticate_candidate(
            base_url=args.base_url,
            candidate=candidate,
            insecure_tls=args.insecure_tls,
        ):
            successes.append(candidate)

    evidence = {
        "schema": "current-web-credential-resolution-v3",
        "mutation_executed": False,
        "secret_values_emitted": False,
        "device_host_bound": True,
        "web_endpoint_host_bound": bool(web_host),
        "credential_candidates": len(candidates),
        "successful_candidates": len(successes),
        "secret_metadata_mode": metadata_mode,
        "resolved_device_credential_candidate_present": bool(env_username and env_password),
        "secret_allowed_host_candidate_count": len(secret_candidates),
        "candidate_source_paths": [list(item.sources) for item in candidates],
        "secret_schema_metadata": schema_metadata,
        "selection": "EXACTLY_ONE_AUTHENTICATED_CANDIDATE",
        "selected_source_paths": list(successes[0].sources) if len(successes) == 1 else [],
    }
    return (successes[0] if len(successes) == 1 else None), evidence


def _write_env(path: Path, candidate: _Candidate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "WEB_USERNAME=" + shlex.quote(candidate.username) + "\n"
        + "WEB_PASSWORD=" + shlex.quote(candidate.password) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve one proven current WEB credential without guessing")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--device-host", required=True)
    parser.add_argument("--secret-file", default="/home/dev/secret.yaml")
    parser.add_argument("--username-env", default="SIP_ABA_SSH_USERNAME")
    parser.add_argument("--password-env", default="SIP_ABA_SSH_PASSWORD")
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--insecure-tls", action="store_true")
    args = parser.parse_args()

    selected, evidence = asyncio.run(_resolve(args))
    evidence_path = Path(args.evidence_output)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "WEB_CREDENTIAL_RESOLUTION": "PASS" if selected is not None else "BLOCKED",
        "mutation": False,
        "secret_values_emitted": False,
        "candidate_count": evidence["credential_candidates"],
        "success_count": evidence["successful_candidates"],
    }, sort_keys=True))
    if selected is None:
        return 3
    _write_env(Path(args.output), selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

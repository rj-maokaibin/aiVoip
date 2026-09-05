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


def _read_secret_text(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", "absent"
    try:
        return path.read_text(encoding="utf-8", errors="ignore"), "runner"
    except PermissionError:
        completed = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return "", "unreadable"
        return completed.stdout, "sudo_pipe"


def _matching_secret_candidates(secret_text: str, *, device_host: str) -> list[_Candidate]:
    if not secret_text:
        return []
    try:
        root = yaml.safe_load(secret_text) or {}
    except Exception:
        return []
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
                if host == device_host and username and password:
                    found.append(_Candidate(username, password, (path or "<root>",)))
            for key, child in value.items():
                key_text = str(key)
                walk(child, f"{path}.{key_text}" if path else key_text)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]" if path else f"[{index}]")

    walk(root)
    return found


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

    secret_text, metadata_mode = _read_secret_text(Path(args.secret_file))
    try:
        candidates.extend(
            _matching_secret_candidates(secret_text, device_host=args.device_host)
        )
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
        "schema": "current-web-credential-resolution-v1",
        "mutation_executed": False,
        "secret_values_emitted": False,
        "device_host_bound": True,
        "credential_candidates": len(candidates),
        "successful_candidates": len(successes),
        "secret_metadata_mode": metadata_mode,
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

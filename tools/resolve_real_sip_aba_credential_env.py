#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path


PRODUCTION_PROJECT = "aivoip"
PRODUCTION_CREDENTIAL_SERVICE = "reproduction-worker"

_CONTAINER_SCRIPT = r'''
import asyncio
import json
import os

from app.integrations.credentials import get_credential_provider

async def main():
    provider = get_credential_provider()
    provider_id = str(getattr(provider, "provider_id", type(provider).__name__)).strip().lower()
    expected = os.environ.get("EXPECTED_PROVIDER", "").strip().lower()
    if not bool(getattr(provider, "production_capable", False)):
        raise SystemExit("PROVIDER_NOT_PRODUCTION_CAPABLE")
    if expected and provider_id != expected:
        raise SystemExit("PROVIDER_ID_MISMATCH")
    sn = os.environ["TARGET_SN"]
    ip = os.environ["TARGET_IP"]
    product = os.environ.get("TARGET_PRODUCT") or None
    password = await provider.get_password(sn=sn, ip=ip, product=product)
    if not str(password or ""):
        raise SystemExit("PROVIDER_EMPTY_PASSWORD")
    fallback = os.environ.get("TARGET_FALLBACK_USER", "").strip() or "root"
    username = fallback
    resolver = getattr(provider, "resolve_username", None)
    if callable(resolver):
        username = str(resolver(ip=ip, fallback=fallback) or "").strip() or "root"
    print(json.dumps({"provider": provider_id, "password": str(password), "username": username}))

asyncio.run(main())
'''


def _inspect_container(name: str, expected_provider: str) -> None:
    data = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
    if not bool((data.get("State") or {}).get("Running")):
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_RUNTIME_NOT_RUNNING")
    env: dict[str, str] = {}
    for item in ((data.get("Config") or {}).get("Env") or []):
        key, sep, value = str(item).partition("=")
        if sep:
            env[key] = value
    labels = dict((data.get("Config") or {}).get("Labels") or {})
    if str(env.get("APP_ENV") or "").strip().lower() != "production":
        raise SystemExit("SIP_ABA_CREDENTIAL_RUNTIME_NOT_PRODUCTION")
    if str(env.get("REPRODUCTION_PLATFORM_MODE") or "").strip().lower() != "real":
        raise SystemExit("SIP_ABA_CREDENTIAL_RUNTIME_NOT_REAL_MODE")
    if str(labels.get("com.docker.compose.project") or "") != PRODUCTION_PROJECT:
        raise SystemExit("SIP_ABA_CREDENTIAL_RUNTIME_PROJECT_MISMATCH")
    if str(labels.get("com.docker.compose.service") or "") != PRODUCTION_CREDENTIAL_SERVICE:
        raise SystemExit("SIP_ABA_CREDENTIAL_RUNTIME_SERVICE_MISMATCH")
    observed_provider = str(env.get("CREDENTIAL_PROVIDER") or "").strip().lower()
    if not observed_provider or observed_provider != expected_provider.strip().lower():
        raise SystemExit("SIP_ABA_CREDENTIAL_RUNTIME_PROVIDER_MISMATCH")


def _resolve_in_production_runtime(
    *, container: str, expected_provider: str, sn: str, ip: str, product: str, fallback_user: str
) -> dict[str, str]:
    _inspect_container(container, expected_provider)
    cmd = [
        "docker", "exec", "-i",
        "-e", f"EXPECTED_PROVIDER={expected_provider}",
        "-e", f"TARGET_SN={sn}",
        "-e", f"TARGET_IP={ip}",
        "-e", f"TARGET_PRODUCT={product}",
        "-e", f"TARGET_FALLBACK_USER={fallback_user}",
        container, "python", "-",
    ]
    proc = subprocess.run(cmd, input=_CONTAINER_SCRIPT, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"SIP_ABA_PRODUCTION_CREDENTIAL_RESOLUTION_FAILED rc={proc.returncode}"
        )
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_RESPONSE_INVALID") from exc
    provider = str(payload.get("provider") or "").strip().lower()
    password = str(payload.get("password") or "")
    username = str(payload.get("username") or "").strip()
    if provider != expected_provider.strip().lower():
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_PROVIDER_CHANGED")
    if not password:
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_EMPTY_PASSWORD")
    if not username:
        raise SystemExit("SIP_ABA_PRODUCTION_CREDENTIAL_EMPTY_USERNAME")
    return {"provider": provider, "password": password, "username": username}


def _write_private_env(path: Path, payload: dict[str, str]) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise SystemExit("SIP_ABA_RUNTIME_DIRECTORY_NOT_PRIVATE")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        body = (
            f"SIP_ABA_SSH_PASSWORD={shlex.quote(payload['password'])}\n"
            f"SIP_ABA_SSH_USERNAME={shlex.quote(payload['username'])}\n"
            f"SIP_ABA_EFFECTIVE_CREDENTIAL_PROVIDER={shlex.quote(payload['provider'])}\n"
        )
        os.write(fd, body.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--expected-provider", required=True)
    parser.add_argument("--sn", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--fallback-user", default="root")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = _resolve_in_production_runtime(
        container=args.container,
        expected_provider=args.expected_provider,
        sn=args.sn,
        ip=args.ip,
        product=args.product,
        fallback_user=args.fallback_user,
    )
    _write_private_env(Path(args.output), payload)
    print(f"SIP_ABA_PRODUCTION_CREDENTIAL_PROVIDER={payload['provider']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

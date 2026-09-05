#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.adapters.web_auth.apf3260m import build_apf3260m_luci_auth_provider
from app.automation.adapters.web_auth.base import SessionManager, WebCredential
from app.automation.adapters.web_profiles.schema import WebApiProfile
from app.automation.gates.golden_web_config import config_payload_from_web_module
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.transport.http import HttpApiTransport
from app.infrastructure.transport.ssh import SharedSshTransport


_SAFE_UCI_KEY = re.compile(r"^[A-Za-z0-9_@.\[\]-]+$")
_USER_TOKENS = {"username", "user", "login", "account", "admin"}
_PASS_TOKENS = {"password", "passwd", "pwd", "pass"}


@dataclass(frozen=True)
class _Candidate:
    username: str
    password: str = field(repr=False, compare=False)
    user_key: str = ""
    password_key: str = ""


def _env_password(ref: str) -> None:
    if not ref.startswith("ENV:"):
        raise RuntimeError("SSH_PASSWORD_ENV_REFERENCE_REQUIRED")
    name = ref.removeprefix("ENV:")
    if not name or not os.getenv(name):
        raise RuntimeError("SSH_PASSWORD_ENV_MISSING")


def _leaf_tokens(key: str) -> set[str]:
    leaf = key.rsplit(".", 1)[-1].lower()
    parts = {part for part in re.split(r"[^a-z0-9]+", leaf) if part}
    parts.add(leaf)
    return parts


def _credential_pairs(keys: list[str]) -> list[tuple[str, str]]:
    sections: dict[str, dict[str, list[str]]] = {}
    for key in keys:
        if "." not in key or not _SAFE_UCI_KEY.fullmatch(key):
            continue
        section = key.rsplit(".", 1)[0]
        tokens = _leaf_tokens(key)
        entry = sections.setdefault(section, {"user": [], "password": []})
        if tokens & _USER_TOKENS:
            entry["user"].append(key)
        if tokens & _PASS_TOKENS:
            entry["password"].append(key)
    pairs: list[tuple[str, str]] = []
    for entry in sections.values():
        if len(entry["user"]) == 1 and len(entry["password"]) == 1:
            pairs.append((entry["user"][0], entry["password"][0]))
    return sorted(set(pairs))


async def _authenticate_and_match(
    *,
    base_url: str,
    candidate: _Candidate,
    profile: WebApiProfile,
    ssh_voip_user,
    insecure_tls: bool,
) -> tuple[bool, bool]:
    client = httpx.AsyncClient(base_url=base_url, verify=not insecure_tls)
    transport = HttpApiTransport(base_url, client=client)
    provider = build_apf3260m_luci_auth_provider(
        timestamp_provider=lambda: str(int(time.time()))
    )
    session = SessionManager(
        transport,
        provider,
        lambda: WebCredential(username=candidate.username, password=candidate.password),
    )
    web = WebEntryAdapter(profile=profile, session_manager=session)
    try:
        await session.ensure_session()
        read = await web.read_voip_account(0)
        if not read.accepted or not isinstance(read.runtime_output, dict):
            return True, False
        modules = read.runtime_output.get("modules")
        if not isinstance(modules, dict) or "voipUserInfo" not in modules:
            return True, False
        expected = config_payload_from_web_module(modules["voipUserInfo"])
        return True, bool(
            ConfigFrameworkExecutor.payload_matches_readback(expected, ssh_voip_user)
        )
    except Exception:
        return False, False
    finally:
        await client.aclose()


async def _probe(args) -> tuple[int, dict, _Candidate | None]:
    _env_password(args.ssh_password_env)
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.ssh_password_env)
    config = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules=("voipUserInfo",),
    )
    await adapter.connect()
    candidate: _Candidate | None = None
    keys: list[str] = []
    pairs: list[tuple[str, str]] = []
    value_read = False
    try:
        ssh_voip_user = await config.get("voipUserInfo", timeout=args.timeout)
        if not ssh_voip_user.success:
            raise RuntimeError("SSH_VOIP_USER_READ_FAILED")
        key_result = await adapter.execute_shell(
            "uci show 2>/dev/null | sed 's/=.*$//' | head -n 5000",
            timeout=args.timeout,
            retries=1,
        )
        if key_result.exit_status not in (0, 1):
            raise RuntimeError(f"UCI_KEY_ENUM_EXIT:{key_result.exit_status}")
        keys = [
            line.strip()
            for line in (key_result.stdout or "").splitlines()
            if line.strip() and _SAFE_UCI_KEY.fullmatch(line.strip())
        ]
        pairs = _credential_pairs(keys)
        # Fail closed: never spray multiple credentials at the login endpoint.
        if len(pairs) == 1:
            user_key, password_key = pairs[0]
            user_result = await adapter.execute_shell(
                "uci -q get " + shlex.quote(user_key), timeout=args.timeout, retries=1
            )
            password_result = await adapter.execute_shell(
                "uci -q get " + shlex.quote(password_key), timeout=args.timeout, retries=1
            )
            if user_result.exit_status == 0 and password_result.exit_status == 0:
                username = (user_result.stdout or "").strip()
                password = (password_result.stdout or "").rstrip("\r\n")
                value_read = True
                if username and password:
                    candidate = _Candidate(
                        username=username,
                        password=password,
                        user_key=user_key,
                        password_key=password_key,
                    )
    finally:
        await adapter.disconnect()

    auth_ok = False
    identity_match = False
    if candidate is not None:
        profile = WebApiProfile.load_yaml(args.profile_path)
        auth_ok, identity_match = await _authenticate_and_match(
            base_url=args.base_url,
            candidate=candidate,
            profile=profile,
            ssh_voip_user=ssh_voip_user,
            insecure_tls=args.insecure_tls,
        )

    user_like = [key for key in keys if _leaf_tokens(key) & _USER_TOKENS]
    pass_like = [key for key in keys if _leaf_tokens(key) & _PASS_TOKENS]
    evidence = {
        "schema": "dut-web-credential-config-probe-v1",
        "read_only": True,
        "mutation_executed": False,
        "secret_values_emitted": False,
        "secret_values_persisted": False,
        "uci_key_count": len(keys),
        "username_like_key_count": len(user_like),
        "password_like_key_count": len(pass_like),
        "candidate_pair_count": len(pairs),
        "candidate_pair_key_paths": [
            {"username_key": user_key, "password_key": password_key}
            for user_key, password_key in pairs[:20]
        ],
        "candidate_value_read": value_read,
        "login_attempt_count": 1 if candidate is not None else 0,
        "authenticated": auth_ok,
        "cross_entry_identity_match": identity_match,
        "target_binding_basis": "DUT_UCI_SOURCE_PLUS_WEB_VOIP_USERINFO_EQUALS_SSH_CONFIG_FRAMEWORK_VOIP_USERINFO",
    }
    return (0 if auth_ok and identity_match else 3), evidence, candidate if auth_ok and identity_match else None


def _write_env(path: Path, candidate: _Candidate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "WEB_USERNAME=" + shlex.quote(candidate.username) + "\n"
        + "WEB_PASSWORD=" + shlex.quote(candidate.password) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe exact DUT UCI WEB credential source read-only")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--ssh-password-env", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--profile-path", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--insecure-tls", action="store_true")
    parser.add_argument("--evidence-output", required=True)
    parser.add_argument("--credential-output")
    args = parser.parse_args()

    rc, evidence, selected = asyncio.run(_probe(args))
    output = Path(args.evidence_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if selected is not None and args.credential_output:
        _write_env(Path(args.credential_output), selected)
    print(json.dumps({
        "DUT_WEB_CREDENTIAL_CONFIG_PROBE": "PASS" if rc == 0 else "BLOCKED",
        "candidate_pair_count": evidence["candidate_pair_count"],
        "login_attempt_count": evidence["login_attempt_count"],
        "authenticated": evidence["authenticated"],
        "cross_entry_identity_match": evidence["cross_entry_identity_match"],
        "mutation": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

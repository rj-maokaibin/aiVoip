#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "deploy/live_acceptance/runtime_contract.json"


def _run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _capture(args: list[str]) -> str:
    return _run(args, capture=True).stdout.strip()


def _docker_inspect(target: str, *, image: bool = False) -> dict:
    kind = "image" if image else "container"
    payload = json.loads(_capture(["docker", kind, "inspect", target]))
    if not payload:
        raise RuntimeError(f"DOCKER_{kind.upper()}_INSPECT_EMPTY")
    return payload[0]


def _load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != "voip-live-acceptance-runtime-v1":
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_CONTRACT_INVALID")
    if int(data.get("schema_version") or 0) != 1:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_SCHEMA_UNSUPPORTED")
    return data


def compute_fingerprint(base_image_id: str, blobs: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"voip-live-acceptance-runtime-v1\0")
    digest.update(base_image_id.encode("utf-8"))
    digest.update(b"\0")
    for name, content in sorted(blobs, key=lambda item: item[0]):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_revision() -> str:
    return _capture(["git", "rev-parse", "HEAD"])


def _discover_real_backend(feishu_secret_file: Path) -> tuple[dict, str, str, list[dict[str, str]]]:
    ids = _capture([
        "docker", "ps", "--filter", "label=com.docker.compose.service=backend", "-q"
    ]).split()
    matches: list[tuple[str, dict]] = []
    for cid in ids:
        info = _docker_inspect(cid)
        env = set(info.get("Config", {}).get("Env") or [])
        if "REPRODUCTION_PLATFORM_MODE=real" in env and "FEISHU_LIVE_ENABLED=true" in env:
            matches.append((cid, info))
    if len(matches) != 1:
        raise RuntimeError(f"EXPECTED_ONE_REAL_FEISHU_BACKEND_FOUND_{len(matches)}")

    cid, info = matches[0]
    networks = list((info.get("NetworkSettings", {}).get("Networks") or {}).keys())
    if not networks:
        raise RuntimeError("REAL_BACKEND_HAS_NO_DOCKER_NETWORK")
    network = networks[0]
    base_image = str(info.get("Config", {}).get("Image") or "").strip()
    if not base_image:
        raise RuntimeError("REAL_BACKEND_IMAGE_MISSING")
    _docker_inspect(base_image, image=True)

    mounts: list[dict[str, str]] = []
    destinations: set[str] = set()
    for mount in info.get("Mounts") or []:
        destination = str(mount.get("Destination") or "")
        source = str(mount.get("Source") or "")
        if destination.startswith("/run/secrets/") and source:
            mounts.append({"source": source, "destination": destination})
            destinations.add(destination)
    if "/run/secrets/feishu_app_secret" not in destinations:
        if not feishu_secret_file.is_file():
            raise RuntimeError("FEISHU_SECRET_FALLBACK_MISSING")
        mounts.append({
            "source": str(feishu_secret_file.resolve()),
            "destination": "/run/secrets/feishu_app_secret",
        })
    return info, network, base_image, mounts


def _runtime_tag(contract: dict, fingerprint: str) -> str:
    version = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(contract.get("runtime_version") or "v1"))
    return f"voip-live-acceptance:{version}-{fingerprint[:16]}"


def _prepare(args: argparse.Namespace) -> int:
    contract_path = args.contract.resolve()
    contract = _load_contract(contract_path)
    backend_info, network, base_image, secret_mounts = _discover_real_backend(args.feishu_secret_file.resolve())
    base_image_info = _docker_inspect(base_image, image=True)
    base_image_id = str(base_image_info.get("Id") or "")
    if not base_image_id:
        raise RuntimeError("BASE_IMAGE_ID_MISSING")

    inputs = []
    for relative in (
        "deploy/live_acceptance/runtime_contract.json",
        "deploy/live_acceptance/Dockerfile",
        str(contract.get("requirements_file") or "backend/requirements.txt"),
    ):
        path = ROOT / relative
        inputs.append((relative, path.read_bytes()))
    fingerprint = compute_fingerprint(base_image_id, inputs)
    runtime_image = _runtime_tag(contract, fingerprint)

    cache_hit = True
    try:
        runtime_info = _docker_inspect(runtime_image, image=True)
        labels = runtime_info.get("Config", {}).get("Labels") or {}
        if labels.get("io.ruijie.voip.live_acceptance.fingerprint") != fingerprint:
            raise RuntimeError("RUNTIME_IMAGE_LABEL_FINGERPRINT_MISMATCH")
    except Exception:
        cache_hit = False
        _run([
            "docker", "build", "--pull=false",
            "--build-arg", f"BASE_IMAGE={base_image}",
            "--label", "io.ruijie.voip.live_acceptance.contract=voip-live-acceptance-runtime-v1",
            "--label", f"io.ruijie.voip.live_acceptance.version={contract.get('runtime_version')}",
            "--label", f"io.ruijie.voip.live_acceptance.fingerprint={fingerprint}",
            "-f", str(ROOT / "deploy/live_acceptance/Dockerfile"),
            "-t", runtime_image,
            str(ROOT),
        ])
        runtime_info = _docker_inspect(runtime_image, image=True)

    context = {
        "schema_version": 1,
        "contract": "voip-live-acceptance-runtime-context-v1",
        "runtime_contract": contract["contract"],
        "runtime_version": contract["runtime_version"],
        "runtime_fingerprint": fingerprint,
        "runtime_image": runtime_image,
        "runtime_image_id": runtime_info.get("Id"),
        "base_image": base_image,
        "base_image_id": base_image_id,
        "backend_container_id": backend_info.get("Id"),
        "docker_network": network,
        "source_revision": _source_revision(),
        "secret_mounts": secret_mounts,
    }
    args.context.parent.mkdir(parents=True, exist_ok=True)
    args.context.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(args.context, 0o600)
    print(json.dumps({
        "status": "PASS",
        "contract": context["runtime_contract"],
        "runtime_version": context["runtime_version"],
        "runtime_fingerprint": fingerprint[:16],
        "runtime_image": runtime_image,
        "cache_hit": cache_hit,
        "docker_network": network,
        "secret_mount_count": len(secret_mounts),
        "source_revision": context["source_revision"],
    }, ensure_ascii=False))
    return 0


def _load_context(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != "voip-live-acceptance-runtime-context-v1":
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_CONTEXT_INVALID")
    return data


def _run_in_runtime(args: argparse.Namespace) -> int:
    context = _load_context(args.context.resolve())
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise RuntimeError("LIVE_ACCEPTANCE_WORKSPACE_MISSING")
    if not args.command:
        raise RuntimeError("LIVE_ACCEPTANCE_COMMAND_MISSING")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("LIVE_ACCEPTANCE_COMMAND_MISSING")

    docker_args = [
        "docker", "run", "--rm",
        "--network", str(context["docker_network"]),
        "--env-file", str(args.env_file.resolve()),
        "-v", f"{workspace}:/workspace",
        "-w", "/workspace",
        "-e", "PYTHONPATH=/workspace/backend:/workspace",
        "-e", "MPLCONFIGDIR=/tmp/voip-live-acceptance-matplotlib",
        "-e", f"LIVE_ACCEPTANCE_RUNTIME_CONTRACT={context['runtime_contract']}",
        "-e", f"LIVE_ACCEPTANCE_RUNTIME_VERSION={context['runtime_version']}",
        "-e", f"LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT={context['runtime_fingerprint']}",
        "-e", f"LIVE_ACCEPTANCE_SOURCE_REVISION={context['source_revision']}",
    ]
    destinations = set()
    for mount in context.get("secret_mounts") or []:
        source = str(mount.get("source") or "")
        destination = str(mount.get("destination") or "")
        if not source or not destination:
            continue
        docker_args += ["-v", f"{source}:{destination}:ro"]
        destinations.add(destination)
    if "/run/secrets/feishu_app_secret" in destinations:
        docker_args += ["-e", "FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret"]
    for item in args.set_env or []:
        if "=" not in item:
            raise RuntimeError(f"INVALID_SET_ENV:{item}")
        docker_args += ["-e", item]
    docker_args += [str(context["runtime_image"]), *command]
    completed = subprocess.run(docker_args, cwd=ROOT)
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reusable VOIP AI live acceptance runtime")
    sub = parser.add_subparsers(dest="action", required=True)

    prepare = sub.add_parser("prepare", help="discover production network and prepare cached runtime image")
    prepare.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    prepare.add_argument("--context", type=Path, required=True)
    prepare.add_argument("--feishu-secret-file", type=Path, required=True)
    prepare.set_defaults(func=_prepare)

    run = sub.add_parser("run", help="run a command inside the prepared live acceptance runtime")
    run.add_argument("--context", type=Path, required=True)
    run.add_argument("--env-file", type=Path, required=True)
    run.add_argument("--workspace", type=Path, default=ROOT)
    run.add_argument("--set-env", action="append", default=[])
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=_run_in_runtime)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(json.dumps({"status": "FAIL", "error_code": "SUBPROCESS_FAILED", "returncode": exc.returncode}, ensure_ascii=False), file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_code": type(exc).__name__, "error_message": str(exc)[:300]}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

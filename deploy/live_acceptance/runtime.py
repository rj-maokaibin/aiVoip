#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "deploy/live_acceptance/runtime_contract.json"
ORCHESTRATOR_VERSION = "1.1.0"


def _run(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=check,
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


def _env_map(info: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in info.get("Config", {}).get("Env") or []:
        text = str(row)
        if "=" in text:
            key, value = text.split("=", 1)
            result[key] = value
    return result


def _network_rows(info: dict) -> dict[str, dict]:
    return dict(info.get("NetworkSettings", {}).get("Networks") or {})


def _network_ip(info: dict, network: str) -> str:
    return str((_network_rows(info).get(network) or {}).get("IPAddress") or "").strip()


def _network_aliases(info: dict, network: str) -> set[str]:
    row = _network_rows(info).get(network) or {}
    return {str(x).lower() for x in (row.get("Aliases") or []) if x}


def _postgres_score(info: dict, hostname: str) -> int:
    labels = info.get("Config", {}).get("Labels") or {}
    env = _env_map(info)
    image = str(info.get("Config", {}).get("Image") or "").lower()
    service = str(labels.get("com.docker.compose.service") or "").lower()
    name = str(info.get("Name") or "").lstrip("/").lower()
    aliases: set[str] = set()
    for network in _network_rows(info):
        aliases.update(_network_aliases(info, network))
    score = 0
    if service == hostname.lower():
        score += 30
    if hostname.lower() in aliases:
        score += 25
    if service in {"postgres", "postgresql", "db", "database"}:
        score += 15
    if "postgres" in image:
        score += 8
    if "postgres" in name:
        score += 5
    if env.get("POSTGRES_DB"):
        score += 3
    if env.get("POSTGRES_USER"):
        score += 2
    return score


def _resolve_hostname_inside_backend(backend_id: str, hostname: str) -> str:
    script = "import socket,sys; print(socket.gethostbyname(sys.argv[1]))"
    try:
        value = _capture(["docker", "exec", backend_id, "python", "-c", script, hostname])
    except Exception:
        return ""
    value = value.strip().splitlines()[-1] if value.strip() else ""
    return value if re.fullmatch(r"[0-9a-fA-F:.]+", value) else ""


def _discover_postgres_route(primary_network: str, backend_info: dict) -> dict:
    database_url = _env_map(backend_info).get("DATABASE_URL", "")
    try:
        hostname = str(urlparse(database_url).hostname or "")
    except Exception:
        hostname = ""
    route = {
        "status": "hostname_missing" if not hostname else "not_found",
        "hostname": hostname,
        "candidate_name": "",
        "candidate_count": 0,
        "running_candidate_count": 0,
        "additional_networks": [],
        "host_overrides": [],
    }
    if not hostname:
        return route

    backend_id = str(backend_info.get("Id") or "")
    resolved = _resolve_hostname_inside_backend(backend_id, hostname)
    if resolved:
        route.update(
            status="backend_dns",
            candidate_name="backend-dns",
            running_candidate_count=1,
            host_overrides=[{"hostname": hostname, "address": resolved, "container_id": "", "container_name": "backend-dns"}],
        )
        return route

    try:
        ids = _capture(["docker", "ps", "-aq"]).split()
    except Exception:
        ids = []
    candidates: list[dict] = []
    for cid in ids:
        if cid == backend_id or backend_id.startswith(cid) or cid.startswith(backend_id):
            continue
        try:
            info = _docker_inspect(cid)
        except Exception:
            continue
        score = _postgres_score(info, hostname)
        if score <= 0:
            continue
        networks = _network_rows(info)
        candidates.append(
            {
                "score": score,
                "id": cid,
                "name": str(info.get("Name") or "").lstrip("/") or cid,
                "running": bool((info.get("State") or {}).get("Running")),
                "networks": networks,
                "info": info,
            }
        )

    route["candidate_count"] = len(candidates)
    running = [row for row in candidates if row["running"] and row["networks"]]
    route["running_candidate_count"] = len(running)
    if not running:
        if candidates:
            route["status"] = "candidate_stopped"
            route["candidate_name"] = sorted(candidates, key=lambda x: (x["score"], x["name"]), reverse=True)[0]["name"]
        return route

    running.sort(key=lambda x: (x["score"], x["name"], x["id"]), reverse=True)
    top_score = running[0]["score"]
    top = [row for row in running if row["score"] == top_score]
    if len(top) != 1:
        route["status"] = "ambiguous"
        route["candidate_name"] = ",".join(sorted(row["name"] for row in top))
        return route

    candidate = top[0]
    route["candidate_name"] = candidate["name"]
    networks = list(candidate["networks"].keys())
    if primary_network in networks:
        target_network = primary_network
    else:
        alias_networks = [n for n in networks if hostname.lower() in _network_aliases(candidate["info"], n)]
        target_network = alias_networks[0] if alias_networks else sorted(networks)[0]
        route["additional_networks"] = [target_network]

    address = _network_ip(candidate["info"], target_network)
    if not address:
        route["status"] = "candidate_no_address"
        return route
    aliases = _network_aliases(candidate["info"], target_network)
    if hostname.lower() not in aliases:
        route["host_overrides"] = [
            {"hostname": hostname, "address": address, "container_id": candidate["id"], "container_name": candidate["name"]}
        ]
    route["status"] = "candidate_same_network" if target_network == primary_network else "candidate_cross_network"
    return route


def _discover_real_backend(feishu_secret_file: Path) -> tuple[dict, str, str, list[dict[str, str]]]:
    ids = _capture(["docker", "ps", "--filter", "label=com.docker.compose.service=backend", "-q"]).split()
    matches: list[tuple[str, dict]] = []
    for cid in ids:
        info = _docker_inspect(cid)
        env = set(info.get("Config", {}).get("Env") or [])
        if "REPRODUCTION_PLATFORM_MODE=real" in env and "FEISHU_LIVE_ENABLED=true" in env:
            matches.append((cid, info))
    if len(matches) != 1:
        raise RuntimeError(f"EXPECTED_ONE_REAL_FEISHU_BACKEND_FOUND_{len(matches)}")
    _, info = matches[0]
    networks = list(_network_rows(info).keys())
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
        mounts.append({"source": str(feishu_secret_file.resolve()), "destination": "/run/secrets/feishu_app_secret"})
    return info, network, base_image, mounts


def _runtime_tag(contract: dict, fingerprint: str) -> str:
    version = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(contract.get("runtime_version") or "v1"))
    return f"voip-live-acceptance:{version}-{fingerprint[:16]}"


def _prepare(args: argparse.Namespace) -> int:
    contract_path = args.contract.resolve()
    contract = _load_contract(contract_path)
    backend_info, network, base_image, secret_mounts = _discover_real_backend(args.feishu_secret_file.resolve())
    database_route = _discover_postgres_route(network, backend_info)
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
        _run(
            [
                "docker", "build", "--pull=false",
                "--build-arg", f"BASE_IMAGE={base_image}",
                "--label", "io.ruijie.voip.live_acceptance.contract=voip-live-acceptance-runtime-v1",
                "--label", f"io.ruijie.voip.live_acceptance.version={contract.get('runtime_version')}",
                "--label", f"io.ruijie.voip.live_acceptance.fingerprint={fingerprint}",
                "-f", str(ROOT / "deploy/live_acceptance/Dockerfile"),
                "-t", runtime_image,
                str(ROOT),
            ]
        )
        runtime_info = _docker_inspect(runtime_image, image=True)
    context = {
        "schema_version": 1,
        "contract": "voip-live-acceptance-runtime-context-v1",
        "runtime_contract": contract["contract"],
        "runtime_version": contract["runtime_version"],
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "runtime_fingerprint": fingerprint,
        "runtime_image": runtime_image,
        "runtime_image_id": runtime_info.get("Id"),
        "base_image": base_image,
        "base_image_id": base_image_id,
        "backend_container_id": backend_info.get("Id"),
        "docker_network": network,
        "source_revision": _source_revision(),
        "secret_mounts": secret_mounts,
        "host_overrides": database_route.get("host_overrides") or [],
        "additional_networks": database_route.get("additional_networks") or [],
        "database_route": database_route,
    }
    args.context.parent.mkdir(parents=True, exist_ok=True)
    args.context.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(args.context, 0o600)
    print(
        json.dumps(
            {
                "status": "PASS",
                "contract": context["runtime_contract"],
                "runtime_version": context["runtime_version"],
                "orchestrator_version": ORCHESTRATOR_VERSION,
                "runtime_fingerprint": fingerprint[:16],
                "runtime_image": runtime_image,
                "cache_hit": cache_hit,
                "docker_network": network,
                "secret_mount_count": len(secret_mounts),
                "database_route_status": database_route.get("status"),
                "database_candidate": database_route.get("candidate_name"),
                "database_candidate_count": database_route.get("candidate_count"),
                "database_running_candidate_count": database_route.get("running_candidate_count"),
                "additional_networks": database_route.get("additional_networks") or [],
                "host_override_count": len(database_route.get("host_overrides") or []),
                "source_revision": context["source_revision"],
            },
            ensure_ascii=False,
        )
    )
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
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("LIVE_ACCEPTANCE_COMMAND_MISSING")
    backend_id = str(context.get("backend_container_id") or "")
    backend_info = _docker_inspect(backend_id)
    if not bool((backend_info.get("State") or {}).get("Running")):
        raise RuntimeError("LIVE_BACKEND_NOT_RUNNING")
    workspace_revision = _source_revision()
    if workspace_revision != str(context.get("source_revision") or ""):
        raise RuntimeError("LIVE_ACCEPTANCE_WORKSPACE_REVISION_DRIFT")

    runtime_env_path: Path | None = None
    container_name = f"voip-live-acceptance-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    created = False
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="voip-live-runtime-env-", delete=False) as fh:
            runtime_env_path = Path(fh.name)
            for row in backend_info.get("Config", {}).get("Env") or []:
                if "=" in str(row):
                    fh.write(str(row) + "\n")
        os.chmod(runtime_env_path, 0o600)
        docker_args = [
            "docker", "create", "--name", container_name,
            "--network", str(context["docker_network"]),
            "--env-file", str(args.env_file.resolve()),
            "--env-file", str(runtime_env_path),
            "-v", f"{workspace}:/workspace",
            "-w", "/workspace",
            "-e", "PYTHONPATH=/workspace/backend:/workspace",
            "-e", "MPLCONFIGDIR=/tmp/voip-live-acceptance-matplotlib",
            "-e", f"LIVE_ACCEPTANCE_RUNTIME_CONTRACT={context['runtime_contract']}",
            "-e", f"LIVE_ACCEPTANCE_RUNTIME_VERSION={context['runtime_version']}",
            "-e", f"LIVE_ACCEPTANCE_ORCHESTRATOR_VERSION={context.get('orchestrator_version') or ORCHESTRATOR_VERSION}",
            "-e", f"LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT={context['runtime_fingerprint']}",
            "-e", f"LIVE_ACCEPTANCE_SOURCE_REVISION={context['source_revision']}",
            "-e", f"LIVE_ACCEPTANCE_WORKSPACE_REVISION={workspace_revision}",
            "-e", f"LIVE_ACCEPTANCE_DATABASE_ROUTE_STATUS={(context.get('database_route') or {}).get('status','')}",
        ]
        for override in context.get("host_overrides") or []:
            hostname = str(override.get("hostname") or "").strip()
            address = str(override.get("address") or "").strip()
            if hostname and address:
                docker_args += ["--add-host", f"{hostname}:{address}"]
        destinations = set()
        for mount in context.get("secret_mounts") or []:
            source = str(mount.get("source") or "")
            destination = str(mount.get("destination") or "")
            if source and destination:
                docker_args += ["-v", f"{source}:{destination}:ro"]
                destinations.add(destination)
        if "/run/secrets/feishu_app_secret" in destinations:
            docker_args += ["-e", "FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret"]
        for item in args.set_env or []:
            if "=" not in item:
                raise RuntimeError(f"INVALID_SET_ENV:{item}")
            docker_args += ["-e", item]
        docker_args += [str(context["runtime_image"]), *command]
        _capture(docker_args)
        created = True
        for network in context.get("additional_networks") or []:
            if network and network != context.get("docker_network"):
                _run(["docker", "network", "connect", str(network), container_name])
        _run(["docker", "start", "-a", container_name], check=False)
        state = _docker_inspect(container_name).get("State") or {}
        return int(state.get("ExitCode") or 0)
    finally:
        if created:
            _run(["docker", "rm", "-f", container_name], check=False)
        if runtime_env_path is not None:
            try:
                runtime_env_path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Reusable VOIP AI live acceptance runtime")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="discover production topology and prepare cached runtime image")
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

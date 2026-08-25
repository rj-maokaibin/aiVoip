#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
V1_RUNTIME_PATH = ROOT / "deploy/live_acceptance/runtime.py"
DEFAULT_CONTRACT = ROOT / "deploy/live_acceptance/runtime_contract_v2.json"
RUNTIME_CONTRACT = "voip-live-acceptance-runtime-v2"
CONTEXT_CONTRACT = "voip-live-acceptance-runtime-context-v2"
ORCHESTRATOR_VERSION = "2.0.0"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"MODULE_LOAD_FAILED:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


v1 = _load_module(V1_RUNTIME_PATH, "voip_live_acceptance_runtime_v1_compat")


def _load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != RUNTIME_CONTRACT:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_CONTRACT_INVALID")
    if int(data.get("schema_version") or 0) != 2:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_SCHEMA_UNSUPPORTED")
    if str(data.get("runtime_version") or "") != "2.0.0":
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_VERSION_INVALID")
    compatibility = data.get("compatibility") or {}
    if compatibility.get("v1_runtime_preserved") is not True:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_V1_COMPAT_REQUIRED")
    return data


def compute_fingerprint(
    base_image_id: str,
    blobs: Iterable[tuple[str, bytes]],
    *,
    contract_id: str = RUNTIME_CONTRACT,
) -> str:
    digest = hashlib.sha256()
    digest.update(contract_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(base_image_id.encode("utf-8"))
    digest.update(b"\0")
    for name, content in sorted(blobs, key=lambda item: item[0]):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_tag(contract: dict, fingerprint: str) -> str:
    version = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(contract.get("runtime_version") or "v2"))
    return f"voip-live-acceptance:{version}-{fingerprint[:16]}"


def _contract_input_name(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return path.name


def _prepare(args: argparse.Namespace) -> int:
    contract_path = args.contract.resolve()
    contract = _load_contract(contract_path)
    backend_info, network, base_image, secret_mounts = v1._discover_real_backend(args.feishu_secret_file.resolve())
    database_route = v1._discover_postgres_route(network, backend_info)
    base_image_info = v1._docker_inspect(base_image, image=True)
    base_image_id = str(base_image_info.get("Id") or "")
    if not base_image_id:
        raise RuntimeError("BASE_IMAGE_ID_MISSING")

    requirements = ROOT / str(contract.get("requirements_file") or "backend/requirements.txt")
    dockerfile = ROOT / str(contract.get("runtime_dockerfile") or "deploy/live_acceptance/Dockerfile")
    inputs = [
        (_contract_input_name(contract_path), contract_path.read_bytes()),
        (_contract_input_name(dockerfile), dockerfile.read_bytes()),
        (_contract_input_name(requirements), requirements.read_bytes()),
    ]
    fingerprint = compute_fingerprint(base_image_id, inputs)
    runtime_image = _runtime_tag(contract, fingerprint)
    cache_hit = True

    try:
        runtime_info = v1._docker_inspect(runtime_image, image=True)
        labels = runtime_info.get("Config", {}).get("Labels") or {}
        if labels.get("io.ruijie.voip.live_acceptance.fingerprint") != fingerprint:
            raise RuntimeError("RUNTIME_IMAGE_LABEL_FINGERPRINT_MISMATCH")
        if labels.get("io.ruijie.voip.live_acceptance.contract") != RUNTIME_CONTRACT:
            raise RuntimeError("RUNTIME_IMAGE_LABEL_CONTRACT_MISMATCH")
    except Exception:
        cache_hit = False
        v1._run(
            [
                "docker",
                "build",
                "--pull=false",
                "--build-arg",
                f"BASE_IMAGE={base_image}",
                "--label",
                f"io.ruijie.voip.live_acceptance.contract={RUNTIME_CONTRACT}",
                "--label",
                f"io.ruijie.voip.live_acceptance.version={contract.get('runtime_version')}",
                "--label",
                f"io.ruijie.voip.live_acceptance.fingerprint={fingerprint}",
                "-f",
                str(dockerfile),
                "-t",
                runtime_image,
                str(ROOT),
            ]
        )
        runtime_info = v1._docker_inspect(runtime_image, image=True)

    context = {
        "schema_version": 2,
        "contract": CONTEXT_CONTRACT,
        "runtime_contract": contract["contract"],
        "runtime_version": contract["runtime_version"],
        "acceptance_infrastructure_version": contract.get("acceptance_infrastructure_version", "2.0"),
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "runtime_fingerprint": fingerprint,
        "runtime_image": runtime_image,
        "runtime_image_id": runtime_info.get("Id"),
        "base_image": base_image,
        "base_image_id": base_image_id,
        "backend_container_id": backend_info.get("Id"),
        "docker_network": network,
        "source_revision": v1._source_revision(),
        "secret_mounts": secret_mounts,
        "host_overrides": database_route.get("host_overrides") or [],
        "additional_networks": database_route.get("additional_networks") or [],
        "database_route": database_route,
        "compatibility": {
            "delegates_safe_runtime_primitives_to": "voip-live-acceptance-runtime-v1",
            "v1_evidence_valid": True,
        },
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
                "acceptance_infrastructure_version": context["acceptance_infrastructure_version"],
                "orchestrator_version": ORCHESTRATOR_VERSION,
                "runtime_fingerprint": fingerprint[:16],
                "runtime_image": runtime_image,
                "cache_hit": cache_hit,
                "docker_network": network,
                "secret_mount_count": len(secret_mounts),
                "database_route_status": database_route.get("status"),
                "database_candidate": database_route.get("candidate_name"),
                "database_candidate_project": database_route.get("candidate_project"),
                "database_candidate_state": database_route.get("candidate_state"),
                "database_candidate_exit_code": database_route.get("candidate_exit_code"),
                "database_candidate_count": database_route.get("candidate_count"),
                "database_trusted_candidate_count": database_route.get("trusted_candidate_count"),
                "database_external_candidate_count": database_route.get("external_candidate_count"),
                "excluded_transient_count": database_route.get("excluded_transient_count"),
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
    if data.get("contract") != CONTEXT_CONTRACT:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_CONTEXT_INVALID")
    if int(data.get("schema_version") or 0) != 2:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_CONTEXT_SCHEMA_UNSUPPORTED")
    if data.get("runtime_contract") != RUNTIME_CONTRACT:
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_V2_CONTEXT_CONTRACT_MISMATCH")
    return data


def _delegate_with_v2_context(func, args: argparse.Namespace) -> int:
    original = v1._load_context
    v1._load_context = _load_context
    try:
        return int(func(args))
    finally:
        v1._load_context = original


def _recover_database(args: argparse.Namespace) -> int:
    return _delegate_with_v2_context(v1._recover_database, args)


def _run_in_runtime(args: argparse.Namespace) -> int:
    return _delegate_with_v2_context(v1._run_in_runtime, args)


def main() -> int:
    parser = argparse.ArgumentParser(description="VOIP AI Acceptance Infrastructure V2 runtime")
    sub = parser.add_subparsers(dest="action", required=True)

    prepare = sub.add_parser("prepare", help="prepare a cached V2 live acceptance runtime")
    prepare.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    prepare.add_argument("--context", type=Path, required=True)
    prepare.add_argument("--feishu-secret-file", type=Path, required=True)
    prepare.set_defaults(func=_prepare)

    recover = sub.add_parser("recover-database", help="reuse the guarded V1 database recovery primitive")
    recover.add_argument("--context", type=Path, required=True)
    recover.set_defaults(func=_recover_database)

    run = sub.add_parser("run", help="run a command inside the prepared V2 acceptance runtime")
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
        print(
            json.dumps(
                {"status": "FAIL", "error_code": "SUBPROCESS_FAILED", "returncode": exc.returncode},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return exc.returncode or 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error_code": type(exc).__name__, "error_message": str(exc)[:300]},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

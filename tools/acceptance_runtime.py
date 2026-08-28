#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("VOIP_ACCEPTANCE_ROOT", "/opt/voip-acceptance"))
RUNTIME_IMAGE = "voip-acceptance-runtime:v2.0.0"
DOCKER_IMAGES = [
    "postgres:16",
    "redis:7-alpine",
    "minio/minio:RELEASE.2025-04-22T22-12-26Z",
]
INPUTS = [
    "backend/requirements.txt",
    "frontend/package-lock.json",
    "deploy/acceptance_v2/contract.json",
    "deploy/acceptance_v2/Dockerfile",
]


def run(command: list[str], timeout: int = 900, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd or REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def fingerprint() -> str:
    h = hashlib.sha256()
    for rel in sorted(INPUTS):
        path = REPO_ROOT / rel
        h.update(rel.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def paths(root: Path) -> dict[str, Path]:
    fp = fingerprint()
    return {
        "state": root / "runtime" / "state.json",
        "venv": root / "runtime" / "python" / fp[:16] / "venv",
        "npm_cache": root / "runtime" / "npm-cache" / fp[:16],
    }


def inspect_image(image: str) -> bool:
    return run(["docker", "image", "inspect", image], timeout=30).returncode == 0


def verify(root: Path) -> tuple[bool, dict]:
    p = paths(root)
    fp = fingerprint()
    errors: list[str] = []
    if not p["state"].is_file():
        errors.append("RUNTIME_STATE_MISSING")
        data = {}
    else:
        try:
            data = json.loads(p["state"].read_text(encoding="utf-8"))
        except Exception:
            data = {}
            errors.append("RUNTIME_STATE_INVALID")
    if data.get("fingerprint") != fp:
        errors.append("RUNTIME_FINGERPRINT_STALE")
    python = p["venv"] / "bin" / "python"
    if not python.is_file():
        errors.append("PYTHON_VENV_MISSING")
    elif run([str(python), "-m", "pip", "check"], timeout=60).returncode != 0:
        errors.append("PYTHON_VENV_BROKEN")
    if not p["npm_cache"].is_dir() or not any(p["npm_cache"].iterdir()):
        errors.append("NPM_CACHE_MISSING")
    if not inspect_image(RUNTIME_IMAGE):
        errors.append("ACCEPTANCE_RUNTIME_IMAGE_MISSING")
    for image in DOCKER_IMAGES:
        if not inspect_image(image):
            errors.append("DOCKER_IMAGE_MISSING:" + image)
    payload = {
        "schema_version": 1,
        "contract": "voip-acceptance-prepared-runtime-v1",
        "status": "PASS" if not errors else "FAIL",
        "fingerprint": fp,
        "venv": str(p["venv"]),
        "npm_cache": str(p["npm_cache"]),
        "runtime_image": RUNTIME_IMAGE,
        "errors": errors,
    }
    return not errors, payload


def prepare(root: Path) -> dict:
    p = paths(root)
    fp = fingerprint()
    p["venv"].parent.mkdir(parents=True, exist_ok=True)
    p["npm_cache"].mkdir(parents=True, exist_ok=True)
    if not (p["venv"] / "bin" / "python").is_file():
        result = run(["python3", "-m", "venv", str(p["venv"])], timeout=120)
        if result.returncode != 0:
            raise RuntimeError("VENV_CREATE_FAILED:" + result.stdout[-500:])
    result = run([str(p["venv"] / "bin" / "python"), "-m", "pip", "install", "--upgrade", "pip"], timeout=300)
    if result.returncode != 0:
        raise RuntimeError("PIP_BOOTSTRAP_FAILED:" + result.stdout[-500:])
    result = run([str(p["venv"] / "bin" / "pip"), "install", "-r", "backend/requirements.txt"], timeout=900)
    if result.returncode != 0:
        raise RuntimeError("PIP_REQUIREMENTS_FAILED:" + result.stdout[-500:])
    result = run(["npm", "ci", "--cache", str(p["npm_cache"])], timeout=900, cwd=REPO_ROOT / "frontend")
    if result.returncode != 0:
        raise RuntimeError("NPM_CACHE_PREPARE_FAILED:" + result.stdout[-500:])
    shutil.rmtree(REPO_ROOT / "frontend" / "node_modules", ignore_errors=True)
    for image in DOCKER_IMAGES:
        result = run(["docker", "pull", image], timeout=900)
        if result.returncode != 0:
            raise RuntimeError("DOCKER_PULL_FAILED:" + image + ":" + result.stdout[-500:])
    result = run(["docker", "build", "-t", RUNTIME_IMAGE, "-f", "deploy/acceptance_v2/Dockerfile", "."], timeout=1800)
    if result.returncode != 0:
        raise RuntimeError("RUNTIME_IMAGE_BUILD_FAILED:" + result.stdout[-800:])
    p["state"].parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "contract": "voip-acceptance-prepared-runtime-v1",
        "fingerprint": fp,
        "venv": str(p["venv"]),
        "npm_cache": str(p["npm_cache"]),
        "runtime_image": RUNTIME_IMAGE,
        "docker_images": DOCKER_IMAGES,
    }
    tmp = p["state"].with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p["state"])
    ok, verified = verify(root)
    if not ok:
        raise RuntimeError("PREPARED_RUNTIME_VERIFY_FAILED:" + ",".join(verified["errors"]))
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "verify", "env"])
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    args = parser.parse_args()
    root = Path(args.root)
    try:
        if args.command == "prepare":
            payload = prepare(root)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        ok, payload = verify(root)
        if args.command == "env" and ok:
            print(f"VOIP_AI_PREPARED_VENV={payload['venv']}")
            print(f"NPM_CONFIG_CACHE={payload['npm_cache']}")
            print("NPM_CONFIG_OFFLINE=true")
            print("VOIP_AI_OFFLINE_GATE=1")
            print("ACCEPTANCE_RUNTIME_FINGERPRINT=" + payload["fingerprint"])
            return 0
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

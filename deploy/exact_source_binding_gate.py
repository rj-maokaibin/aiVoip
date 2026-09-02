#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
APP_SERVICES = [
    "backend",
    "collector-worker",
    "packet-worker",
    "pcm-worker",
    "media-worker",
    "diagnosis-worker",
    "feishu-long-connection",
    "reproduction-worker",
    "reproduction-control-high-worker",
    "reproduction-watch-worker",
    "beat",
    "frontend",
]


def _run(args: list[str], *, cwd: Path = ROOT) -> str:
    cp = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return cp.stdout.strip()


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def evaluate_snapshot(snapshot: dict[str, Any], *, phase: str) -> list[str]:
    errors: list[str] = []
    expected = str(snapshot.get("expected_revision") or "")
    if not SHA40.fullmatch(expected):
        errors.append(f"EXPECTED_REVISION_INVALID:{expected or 'missing'}")

    head = str(snapshot.get("git_head") or "")
    if head != expected:
        errors.append(f"GIT_HEAD_MISMATCH:actual={head or 'missing'}:expected={expected or 'missing'}")
    if snapshot.get("tracked_dirty"):
        errors.append("TRACKED_WORKTREE_DIRTY")
    if snapshot.get("source_manifest_status") != "PASS":
        errors.append(f"SOURCE_MANIFEST_{snapshot.get('source_manifest_status') or 'UNKNOWN'}")

    if phase == "runtime":
        services = snapshot.get("services") or {}
        for service in APP_SERVICES:
            row = services.get(service) or {}
            if int(row.get("container_count") or 0) != 1:
                errors.append(f"CONTAINER_COUNT_MISMATCH:{service}:{row.get('container_count')}")
                continue
            if str(row.get("container_revision") or "") != expected:
                errors.append(
                    f"CONTAINER_REVISION_MISMATCH:{service}:"
                    f"{row.get('container_revision') or 'missing'}:{expected}"
                )
            if str(row.get("image_revision") or "") != expected:
                errors.append(
                    f"IMAGE_REVISION_MISMATCH:{service}:"
                    f"{row.get('image_revision') or 'missing'}:{expected}"
                )

        if str(snapshot.get("backend_health_revision") or "") != expected:
            errors.append(
                f"BACKEND_HEALTH_REVISION_MISMATCH:"
                f"{snapshot.get('backend_health_revision') or 'missing'}:{expected}"
            )
        if snapshot.get("runtime_evidence_passed") is not True:
            errors.append("RUNTIME_EVIDENCE_NOT_PASS")
        if str(snapshot.get("runtime_evidence_revision") or "") != expected:
            errors.append(
                f"RUNTIME_EVIDENCE_REVISION_MISMATCH:"
                f"{snapshot.get('runtime_evidence_revision') or 'missing'}:{expected}"
            )
        if snapshot.get("feishu_required"):
            if snapshot.get("feishu_evidence_passed") is not True:
                errors.append("FEISHU_EVIDENCE_NOT_PASS")
            if str(snapshot.get("feishu_evidence_revision") or "") != expected:
                errors.append(
                    f"FEISHU_EVIDENCE_REVISION_MISMATCH:"
                    f"{snapshot.get('feishu_evidence_revision') or 'missing'}:{expected}"
                )
    return errors


def collect_source_snapshot(env_file: Path) -> dict[str, Any]:
    env = parse_env(env_file)
    expected = env.get("BUILD_REVISION", "").strip()
    head = _run(["git", "rev-parse", "HEAD"])
    dirty = bool(_run(["git", "status", "--porcelain", "--untracked-files=no"]))
    manifest = subprocess.run(
        ["python3", "tools/source_manifest_gate.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    manifest_status = "FAIL"
    manifest_payload: dict[str, Any] = {}
    try:
        manifest_payload = json.loads(manifest.stdout or "{}")
        manifest_status = str(manifest_payload.get("status") or "FAIL")
    except json.JSONDecodeError:
        manifest_status = "FAIL"
    return {
        "expected_revision": expected,
        "git_head": head,
        "tracked_dirty": dirty,
        "source_manifest_status": manifest_status,
        "source_manifest_aggregate_sha256": manifest_payload.get("aggregate_sha256"),
    }


def _container_env_revision(container_id: str) -> str:
    raw = _run([
        "docker",
        "inspect",
        "-f",
        "{{range .Config.Env}}{{println .}}{{end}}",
        container_id,
    ])
    for line in raw.splitlines():
        if line.startswith("BUILD_REVISION="):
            return line.split("=", 1)[1]
    return ""


def _image_revision(container_id: str) -> tuple[str, str]:
    image_id = _run(["docker", "inspect", "-f", "{{.Image}}", container_id])
    revision = _run([
        "docker",
        "image",
        "inspect",
        "-f",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
        image_id,
    ])
    return image_id, revision


def collect_runtime_snapshot(
    source: dict[str, Any],
    *,
    project: str,
    backend_url: str,
    runtime_evidence: Path,
    feishu_evidence: Path,
    env_file: Path,
) -> dict[str, Any]:
    snapshot = dict(source)
    services: dict[str, Any] = {}
    for service in APP_SERVICES:
        raw = _run([
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ])
        ids = [x.strip() for x in raw.splitlines() if x.strip()]
        row: dict[str, Any] = {"container_count": len(ids)}
        if len(ids) == 1:
            cid = ids[0]
            image_id, image_revision = _image_revision(cid)
            row.update({
                "container_id_prefix": cid[:12],
                "container_revision": _container_env_revision(cid),
                "image_id": image_id,
                "image_revision": image_revision,
            })
        services[service] = row
    snapshot["services"] = services

    with urllib.request.urlopen(backend_url, timeout=8) as response:
        health = json.loads(response.read().decode("utf-8"))
    snapshot["backend_health_revision"] = health.get("build_revision")

    runtime = json.loads(runtime_evidence.read_text(encoding="utf-8"))
    snapshot["runtime_evidence_passed"] = runtime.get("passed") is True
    snapshot["runtime_evidence_revision"] = runtime.get("build_revision")

    env = parse_env(env_file)
    feishu_required = env.get("FEISHU_LIVE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    snapshot["feishu_required"] = feishu_required
    if feishu_required and feishu_evidence.exists():
        feishu = json.loads(feishu_evidence.read_text(encoding="utf-8"))
        snapshot["feishu_evidence_passed"] = feishu.get("passed") is True
        snapshot["feishu_evidence_revision"] = feishu.get("build_revision")
    elif feishu_required:
        snapshot["feishu_evidence_passed"] = False
        snapshot["feishu_evidence_revision"] = None
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description="Fail-closed VOIP AI exact source binding gate")
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--phase", choices=["source", "runtime"], default="runtime")
    ap.add_argument("--project", default="aivoip")
    ap.add_argument("--backend-url", default="http://127.0.0.1:18001/health/ready")
    ap.add_argument("--runtime-evidence", type=Path, default=ROOT / "validation" / "production_runtime_result.json")
    ap.add_argument("--feishu-evidence", type=Path, default=ROOT / "validation" / "feishu_long_connection_runtime.json")
    ap.add_argument("--out", type=Path, default=ROOT / "validation" / "exact_source_binding_result.json")
    args = ap.parse_args()

    source = collect_source_snapshot(args.env_file.resolve())
    snapshot = source
    if args.phase == "runtime":
        snapshot = collect_runtime_snapshot(
            source,
            project=args.project,
            backend_url=args.backend_url,
            runtime_evidence=args.runtime_evidence.resolve(),
            feishu_evidence=args.feishu_evidence.resolve(),
            env_file=args.env_file.resolve(),
        )
    errors = evaluate_snapshot(snapshot, phase=args.phase)
    payload = {
        "schema_version": 1,
        "gate": "EXACT_SOURCE_BINDING",
        "phase": args.phase,
        "status": "PASS" if not errors else "FAIL",
        "passed": not errors,
        "expected_revision": snapshot.get("expected_revision"),
        "source_manifest_aggregate_sha256": snapshot.get("source_manifest_aggregate_sha256"),
        "errors": errors,
        "snapshot": snapshot,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("VOIP_ACCEPTANCE_ROOT", "/opt/voip-acceptance"))
MANIFEST = REPO_ROOT / "golden_registry/real_offline_001/manifest.json"
COMPOSE = REPO_ROOT / "deploy/acceptance_v2/docker-compose.yml"
RUNTIME_TOOL = REPO_ROOT / "tools/acceptance_runtime.py"
EXPECTED_TSHARK = "4.2.2"


@dataclass
class Probe:
    key: str
    status: str
    detail: str
    classification: str = "NONE"
    repaired: bool = False


def run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)


def add(probes: list[Probe], key: str, ok: bool, detail: str, classification: str = "INFRA_BLOCKED", repaired: bool = False) -> None:
    probes.append(Probe(key, "PASS" if ok else "FAIL", detail[-600:], "NONE" if ok else classification, repaired))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_root(probes: list[Probe], root: Path) -> None:
    try:
        for name in ("golden-cache", "runtime", "state", "logs", "work"):
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            marker = path / ".doctor-write"
            marker.write_text("ok", encoding="utf-8")
            marker.unlink()
        add(probes, "PERSISTENT_ROOT", True, str(root))
    except Exception as exc:
        add(probes, "PERSISTENT_ROOT", False, str(exc))


def check_resources(probes: list[Probe], root: Path) -> None:
    usage = shutil.disk_usage(root if root.exists() else "/")
    free_gb = usage.free / (1024 ** 3)
    add(probes, "DISK_FREE", free_gb >= 10, f"free_gb={free_gb:.1f}")


def check_network(probes: list[Probe], root: Path, deep: bool) -> None:
    try:
        answers = socket.getaddrinfo("github.com", 443, type=socket.SOCK_STREAM)
        add(probes, "GITHUB_DNS", bool(answers), f"answers={len(answers)}", "TRANSIENT_INFRA_RETRYING")
    except Exception as exc:
        add(probes, "GITHUB_DNS", False, str(exc), "TRANSIENT_INFRA_RETRYING")
        return
    try:
        with socket.create_connection(("github.com", 443), timeout=5):
            pass
        add(probes, "GITHUB_TCP_443", True, "connected", "TRANSIENT_INFRA_RETRYING")
    except Exception as exc:
        add(probes, "GITHUB_TCP_443", False, str(exc), "TRANSIENT_INFRA_RETRYING")
        return
    result = run(["git", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=10", "ls-remote", "https://github.com/actions/checkout.git", "HEAD"], timeout=30)
    add(probes, "GITHUB_GIT_REMOTE", result.returncode == 0, result.stdout.strip() or f"rc={result.returncode}", "TRANSIENT_INFRA_RETRYING")
    if deep and result.returncode == 0:
        probe_dir = root / "state" / "github-transfer-probe"
        shutil.rmtree(probe_dir, ignore_errors=True)
        probe_dir.parent.mkdir(parents=True, exist_ok=True)
        result = run(["git", "clone", "--depth=1", "--filter=blob:none", "https://github.com/actions/checkout.git", str(probe_dir)], timeout=90)
        add(probes, "GITHUB_PACK_TRANSFER", result.returncode == 0, result.stdout.strip() or f"rc={result.returncode}", "TRANSIENT_INFRA_RETRYING")
        shutil.rmtree(probe_dir, ignore_errors=True)


def check_docker(probes: list[Probe]) -> bool:
    result = run(["docker", "info"], timeout=15)
    ok = result.returncode == 0
    add(probes, "DOCKER_DAEMON", ok, "docker info ok" if ok else result.stdout)
    return ok


def check_golden(probes: list[Probe], root: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["artifacts"]["pcap"]["sha256"]
    pcap = manifest["artifacts"]["pcap"]
    path = root / "golden-cache" / manifest["golden_id"] / manifest["version"] / pcap["cache_name"]
    if not path.is_file():
        add(probes, "GOLDEN_001", False, f"missing:{path}")
        return
    actual = sha256_file(path)
    add(probes, "GOLDEN_001", actual == expected, f"sha256={actual[:16]}; path={path}")


def check_tshark(probes: list[Probe], root: Path) -> None:
    binary = root / "runtime" / "bin" / "tshark"
    if not binary.is_file():
        add(probes, "TSHARK_RUNTIME", False, f"missing:{binary}")
        return
    result = run([str(binary), "-v"], timeout=10)
    first = (result.stdout.splitlines() or [""])[0]
    add(probes, "TSHARK_RUNTIME", result.returncode == 0 and EXPECTED_TSHARK in first, first or f"rc={result.returncode}")


def check_prepared_runtime(probes: list[Probe], root: Path) -> None:
    result = run([sys.executable, str(RUNTIME_TOOL), "verify", "--root", str(root)], timeout=120)
    detail = result.stdout.strip() or f"rc={result.returncode}"
    add(probes, "PREPARED_RUNTIME", result.returncode == 0, detail)


def compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose", "-p", "voip-acceptance-v2", "-f", str(COMPOSE), *args]


def repair_stack(probes: list[Probe]) -> bool:
    result = run(compose_cmd("up", "-d", "--wait"), timeout=120)
    add(probes, "ACCEPTANCE_STACK_REPAIR", result.returncode == 0, result.stdout or f"rc={result.returncode}", repaired=result.returncode == 0)
    return result.returncode == 0


def check_stack(probes: list[Probe], repair: bool) -> None:
    services = ["postgres", "redis", "minio"]

    def unhealthy() -> list[str]:
        failed: list[str] = []
        for service in services:
            q = run(compose_cmd("ps", "-q", service), timeout=10)
            cid = q.stdout.strip()
            if not cid:
                failed.append(service)
                continue
            inspect = run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cid], timeout=10)
            if inspect.returncode != 0 or inspect.stdout.strip() not in {"healthy", "running"}:
                failed.append(service)
        return failed

    failed = unhealthy()
    if failed and repair and repair_stack(probes):
        failed = unhealthy()
    add(probes, "ACCEPTANCE_STACK", not failed, "healthy" if not failed else "unhealthy=" + ",".join(failed))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--out")
    parser.add_argument("--deep-network", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--require-network", action="store_true")
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument("--require-golden", action="store_true")
    parser.add_argument("--require-tshark", action="store_true")
    parser.add_argument("--require-runtime", action="store_true")
    parser.add_argument("--require-stack", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    probes: list[Probe] = []
    check_root(probes, root)
    check_resources(probes, root)
    if args.require_network:
        check_network(probes, root, args.deep_network)
    docker_ok = True
    if args.require_docker or args.require_stack or args.require_runtime:
        docker_ok = check_docker(probes)
    if args.require_golden:
        check_golden(probes, root)
    if args.require_tshark:
        check_tshark(probes, root)
    if args.require_runtime:
        check_prepared_runtime(probes, root)
    if args.require_stack:
        if docker_ok:
            check_stack(probes, args.repair)
        else:
            add(probes, "ACCEPTANCE_STACK", False, "docker unavailable")
    failures = [p for p in probes if p.status == "FAIL"]
    recovered = any(p.repaired for p in probes)
    transient = any(p.classification == "TRANSIENT_INFRA_RETRYING" for p in failures)
    if failures:
        status = "UNREADY"
        classification = "TRANSIENT_INFRA_RETRYING" if transient else "INFRA_BLOCKED"
    else:
        status = "READY"
        classification = "INFRA_RECOVERED" if recovered else "NONE"
    payload = {
        "schema_version": 1,
        "contract": "voip-runner-doctor-v1",
        "status": status,
        "classification": classification,
        "root": str(root),
        "blocking_keys": [p.key for p in failures],
        "probes": [asdict(p) for p in probes]
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0 if status == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GOLDEN_PCAP = Path("/tmp/tcpdump-2026-08-14.pcap")
_GOLDEN_SHA256 = "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"


def _safe_tail(value: str, limit: int = 16000) -> str:
    lines = []
    for raw in str(value or "").splitlines()[-120:]:
        upper = raw.upper()
        if any(token in upper for token in ("PASSWORD", "SECRET", "TOKEN=", "DATABASE_URL")):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(raw[:1000])
    return "\n".join(lines)[-limit:]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         timeout: float = 600.0, check: bool = False) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        argv,
        cwd=cwd,
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"COMMAND_FAILED:{argv[0]}:rc={cp.returncode}:"
            f"{_safe_tail(cp.stderr or cp.stdout)}"
        )
    return cp


def _git(repo: Path, *args: str, timeout: float = 120.0, check: bool = True) -> str:
    cp = _run(["git", *args], cwd=repo, timeout=timeout, check=False)
    if check and cp.returncode != 0:
        raise RuntimeError(f"GIT_FAILED:{' '.join(args)}:{_safe_tail(cp.stderr)}")
    return cp.stdout.strip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _docker_env(runtime_root: Path, cwd: Path) -> tuple[dict[str, str], str]:
    env = os.environ.copy()
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("DOCKER_NOT_AVAILABLE")
    direct = _run([docker, "info"], cwd=cwd, timeout=30)
    if direct.returncode == 0:
        return env, "DIRECT"

    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("DOCKER_DAEMON_UNAVAILABLE_NO_SUDO")
    probe = _run([sudo, "-n", docker, "info"], cwd=cwd, timeout=30)
    if probe.returncode != 0:
        raise RuntimeError("DOCKER_DAEMON_PERMISSION_DENIED:" + _safe_tail(probe.stderr or probe.stdout))

    shim_dir = runtime_root / "docker-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"sudo={sudo!r}\n"
        f"docker={docker!r}\n"
        "os.execv(sudo, [sudo, '-n', docker, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    return env, "SUDO_NONINTERACTIVE_SHIM"


def _stage(payload: dict[str, Any], *, name: str, argv: list[str], cwd: Path,
           env: dict[str, str], timeout: float) -> bool:
    cp = _run(argv, cwd=cwd, env=env, timeout=timeout)
    payload.setdefault("stages", {})[name] = {
        "return_code": cp.returncode,
        "passed": cp.returncode == 0,
        "stdout_tail": _safe_tail(cp.stdout),
        "stderr_tail": _safe_tail(cp.stderr),
    }
    return cp.returncode == 0


def _find_tshark_422(env: dict[str, str], cwd: Path) -> str | None:
    candidates: list[str] = []
    explicit = str(os.getenv("TSHARK_BINARY") or "").strip()
    if explicit:
        candidates.append(explicit)
    found = shutil.which("tshark", path=env.get("PATH"))
    if found:
        candidates.append(found)
    candidates.append("/tmp/tshark-userspace")
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        path = Path(item)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        cp = _run([str(path), "-v"], cwd=cwd, env=env, timeout=30)
        first = (cp.stdout or cp.stderr).splitlines()[0] if (cp.stdout or cp.stderr) else ""
        if cp.returncode == 0 and "4.2.2" in first:
            return str(path)
    return None


def run(*, repo_root: Path, master_sha: str) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    master_sha = str(master_sha).strip().lower()
    payload: dict[str, Any] = {
        "scope": "ISOLATED_MASTER_BASELINE_MERGE_REGRESSION",
        "master_sha": master_sha,
        "branch_mutation": False,
        "merge_commit_created": False,
        "production_mutation": False,
        "dut_mutation": False,
        "stages": {},
    }
    if not _SHA_RE.fullmatch(master_sha):
        return 1, {**payload, "verdict": "FAIL", "reason": "MASTER_SHA_INVALID"}

    feature_head = _git(repo_root, "rev-parse", "HEAD")
    payload["feature_head"] = feature_head
    runtime_root = Path(tempfile.mkdtemp(prefix="capture-v2-master-baseline-"))
    worktree = runtime_root / "worktree"
    worktree_added = False
    try:
        fetch = _run(["git", "fetch", "--no-tags", "origin", "master"], cwd=repo_root, timeout=180)
        payload["stages"]["fetch_master"] = {
            "return_code": fetch.returncode,
            "passed": fetch.returncode == 0,
            "stdout_tail": _safe_tail(fetch.stdout),
            "stderr_tail": _safe_tail(fetch.stderr),
        }
        if fetch.returncode != 0:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MASTER_FETCH_FAILED"}
        fetched = _git(repo_root, "rev-parse", "FETCH_HEAD")
        payload["fetched_master_sha"] = fetched
        if fetched != master_sha:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MASTER_SHA_MOVED"}

        add = _run(["git", "worktree", "add", "--detach", str(worktree), master_sha], cwd=repo_root, timeout=120)
        payload["stages"]["worktree_add"] = {"return_code": add.returncode, "passed": add.returncode == 0,
                                                  "stderr_tail": _safe_tail(add.stderr)}
        if add.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "WORKTREE_CREATE_FAILED"}
        worktree_added = True

        merge = _run(["git", "merge", "--no-commit", "--no-ff", feature_head], cwd=worktree, timeout=300)
        conflicts = []
        if merge.returncode != 0:
            conflict_cp = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=worktree, timeout=30)
            conflicts = [x.strip() for x in conflict_cp.stdout.splitlines() if x.strip()]
        payload["stages"]["merge_simulation"] = {
            "return_code": merge.returncode,
            "passed": merge.returncode == 0,
            "conflicts": conflicts,
            "stdout_tail": _safe_tail(merge.stdout),
            "stderr_tail": _safe_tail(merge.stderr),
        }
        if merge.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "MASTER_FEATURE_MERGE_CONFLICT"}

        merged_tree = _git(worktree, "write-tree")
        payload["merged_tree"] = merged_tree
        env = os.environ.copy()
        env["CAPTURE_ENGINE_VERSION"] = "V1"
        env["CAPTURE_V2_PRODUCTION_ENABLED"] = "false"
        env["PYTHONPATH"] = "backend:."
        env["RUNNER_TEMP"] = str(runtime_root / "runner-temp")
        Path(env["RUNNER_TEMP"]).mkdir(parents=True, exist_ok=True)

        capture_tests = sorted(str(p.relative_to(worktree)) for p in (worktree / "backend/tests").glob("test_capture_v2_*.py"))
        if not capture_tests:
            return 1, {**payload, "verdict": "FAIL", "reason": "CAPTURE_V2_TESTS_NOT_FOUND_IN_MERGED_TREE"}
        if not _stage(payload, name="capture_v2_regression",
                      argv=[sys.executable, "-m", "pytest", "-q", *capture_tests],
                      cwd=worktree, env=env, timeout=1800):
            return 1, {**payload, "verdict": "FAIL", "reason": "MERGED_CAPTURE_V2_REGRESSION_FAILED"}

        env["PYTHON_BIN"] = sys.executable
        env["PRELIMINARY_EVIDENCE_V1_VENV"] = str(runtime_root / "venv-preliminary")
        if not _stage(payload, name="master_frozen_contracts",
                      argv=["bash", "tools/preliminary_evidence_v1_gate.sh"],
                      cwd=worktree, env=env, timeout=1800):
            return 1, {**payload, "verdict": "FAIL", "reason": "MERGED_MASTER_FROZEN_GATE_FAILED"}

        try:
            env, docker_authority = _docker_env(runtime_root, worktree)
        except Exception as exc:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MERGED_FULL_GATE_DOCKER_UNAVAILABLE",
                       "error": f"{type(exc).__name__}:{_safe_tail(str(exc))}"}
        payload["docker_authority"] = docker_authority
        env["CAPTURE_ENGINE_VERSION"] = "V1"
        env["CAPTURE_V2_PRODUCTION_ENABLED"] = "false"
        env["PYTHONPATH"] = "backend:."
        env["RUNNER_TEMP"] = str(runtime_root / "runner-temp")
        env["PYTHON_BIN"] = sys.executable
        env["VOIP_AI_GATE_VENV"] = str(runtime_root / "voip-ai-release-gate")

        if not _stage(payload, name="master_full_software_acceptance",
                      argv=["bash", "tools/voip_ai_release_gate.sh"],
                      cwd=worktree, env=env, timeout=3600):
            return 1, {**payload, "verdict": "FAIL", "reason": "MERGED_MASTER_FULL_GATE_FAILED"}

        if not _GOLDEN_PCAP.is_file() or not os.access(_GOLDEN_PCAP, os.R_OK):
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "OFFLINE_GOLDEN_PCAP_UNREADABLE"}
        actual_sha = _sha256(_GOLDEN_PCAP)
        payload["offline_golden_pcap_sha256"] = actual_sha
        if actual_sha != _GOLDEN_SHA256:
            return 1, {**payload, "verdict": "FAIL", "reason": "OFFLINE_GOLDEN_PCAP_SHA256_MISMATCH"}

        tshark = _find_tshark_422(env, worktree)
        if not tshark:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "TSHARK_4_2_2_UNAVAILABLE"}
        payload["tshark_binary"] = tshark
        env["TSHARK_BINARY"] = tshark
        result_path = runtime_root / "offline-golden-result.json"
        artifacts = runtime_root / "offline-golden-artifacts"
        replay_python = Path(env["VOIP_AI_GATE_VENV"]) / "bin/python"
        if not _stage(payload, name="real_offline_golden_001",
                      argv=[str(replay_python), "tools/offline_analysis_golden_replay.py",
                            "--pcap", str(_GOLDEN_PCAP), "--require-fixture",
                            "--artifacts", str(artifacts), "--result", str(result_path)],
                      cwd=worktree, env=env, timeout=1200):
            return 1, {**payload, "verdict": "FAIL", "reason": "MERGED_REAL_OFFLINE_GOLDEN_FAILED"}
        try:
            golden = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return 1, {**payload, "verdict": "FAIL", "reason": "OFFLINE_GOLDEN_RESULT_INVALID",
                       "error": type(exc).__name__}
        payload["offline_golden_result"] = {
            "status": golden.get("status"), "passed": golden.get("passed"),
            "checks_passed": golden.get("checks_passed"), "checks_total": golden.get("checks_total"),
        }
        if not (
            golden.get("status") == "PASS" and golden.get("passed") is True
            and golden.get("checks_passed") == 142 and golden.get("checks_total") == 142
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "OFFLINE_GOLDEN_142_CONTRACT_FAILED"}

        payload["verdict"] = "PASS"
        payload["reason"] = "MASTER_BASELINE_MERGE_SIMULATION_FULL_ACCEPTANCE_PASS"
        return 0, payload
    except subprocess.TimeoutExpired as exc:
        payload["verdict"] = "FAIL"
        payload["reason"] = "MASTER_BASELINE_STAGE_TIMEOUT"
        payload["error"] = f"{exc.cmd}"
        return 1, payload
    except Exception as exc:
        payload["verdict"] = "FAIL"
        payload["reason"] = "MASTER_BASELINE_REGRESSION_EXCEPTION"
        payload["error"] = f"{type(exc).__name__}:{_safe_tail(str(exc))}"
        return 1, payload
    finally:
        if worktree_added:
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root, timeout=180)
        shutil.rmtree(runtime_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated PR master-baseline merge regression")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--master-sha", required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, master_sha=args.master_sha)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

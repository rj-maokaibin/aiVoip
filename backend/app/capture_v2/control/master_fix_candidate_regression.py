from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.capture_v2.control.master_baseline_regression import (
    _GOLDEN_PCAP,
    _GOLDEN_SHA256,
    _docker_env,
    _find_tshark_422,
    _git,
    _run,
    _safe_tail,
    _sha256,
    _stage,
)


CANDIDATE_REF = "fix/master-frozen-v2-contract-20260823"
CANDIDATE_SHA = "c962c0d174099bae1afc8db55067402b36717487"
MASTER_ANCESTOR = "e8d108a228b151b7711bfe6048c3f39e94b57783"


def run(*, repo_root: Path, candidate_sha: str) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    payload: dict[str, Any] = {
        "scope": "ISOLATED_MASTER_FIX_CANDIDATE_PLUS_CAPTURE_V2_REGRESSION",
        "candidate_ref": CANDIDATE_REF,
        "candidate_sha": candidate_sha,
        "required_master_ancestor": MASTER_ANCESTOR,
        "branch_mutation": False,
        "merge_commit_created": False,
        "production_mutation": False,
        "dut_mutation": False,
        "stages": {},
    }
    if candidate_sha != CANDIDATE_SHA:
        return 1, {**payload, "verdict": "FAIL", "reason": "MASTER_FIX_CANDIDATE_SHA_NOT_AUDITED"}

    feature_head = _git(repo_root, "rev-parse", "HEAD")
    payload["feature_head"] = feature_head
    runtime_root = Path(tempfile.mkdtemp(prefix="capture-v2-master-fix-candidate-"))
    worktree = runtime_root / "worktree"
    worktree_added = False
    try:
        fetch = _run(
            ["git", "fetch", "--no-tags", "origin", CANDIDATE_REF],
            cwd=repo_root, timeout=180,
        )
        payload["stages"]["fetch_candidate"] = {
            "return_code": fetch.returncode,
            "passed": fetch.returncode == 0,
            "stdout_tail": _safe_tail(fetch.stdout),
            "stderr_tail": _safe_tail(fetch.stderr),
        }
        if fetch.returncode != 0:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MASTER_FIX_CANDIDATE_FETCH_FAILED"}
        fetched = _git(repo_root, "rev-parse", "FETCH_HEAD")
        payload["fetched_candidate_sha"] = fetched
        if fetched != candidate_sha:
            return 2, {**payload, "verdict": "INCONCLUSIVE", "reason": "MASTER_FIX_CANDIDATE_MOVED"}
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor", MASTER_ANCESTOR, candidate_sha],
            cwd=repo_root, timeout=30,
        )
        payload["stages"]["master_ancestor"] = {
            "return_code": ancestor.returncode,
            "passed": ancestor.returncode == 0,
        }
        if ancestor.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_NOT_BASED_ON_AUDITED_MASTER"}

        add = _run(
            ["git", "worktree", "add", "--detach", str(worktree), candidate_sha],
            cwd=repo_root, timeout=120,
        )
        payload["stages"]["worktree_add"] = {
            "return_code": add.returncode,
            "passed": add.returncode == 0,
            "stderr_tail": _safe_tail(add.stderr),
        }
        if add.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "WORKTREE_CREATE_FAILED"}
        worktree_added = True

        merge = _run(
            ["git", "merge", "--no-commit", "--no-ff", feature_head],
            cwd=worktree, timeout=300,
        )
        conflicts: list[str] = []
        if merge.returncode != 0:
            cp = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=worktree, timeout=30)
            conflicts = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
        payload["stages"]["merge_simulation"] = {
            "return_code": merge.returncode,
            "passed": merge.returncode == 0,
            "conflicts": conflicts,
            "stdout_tail": _safe_tail(merge.stdout),
            "stderr_tail": _safe_tail(merge.stderr),
        }
        if merge.returncode != 0:
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_CAPTURE_V2_MERGE_CONFLICT"}

        payload["merged_tree"] = _git(worktree, "write-tree")
        env = os.environ.copy()
        env["CAPTURE_ENGINE_VERSION"] = "V1"
        env["CAPTURE_V2_PRODUCTION_ENABLED"] = "false"
        env["PYTHONPATH"] = "backend:."
        env["RUNNER_TEMP"] = str(runtime_root / "runner-temp")
        Path(env["RUNNER_TEMP"]).mkdir(parents=True, exist_ok=True)

        capture_tests = sorted(
            str(path.relative_to(worktree))
            for path in (worktree / "backend/tests").glob("test_capture_v2_*.py")
        )
        if not _stage(
            payload, name="capture_v2_regression",
            argv=[sys.executable, "-m", "pytest", "-q", *capture_tests],
            cwd=worktree, env=env, timeout=1800,
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_CAPTURE_V2_REGRESSION_FAILED"}

        env["PYTHON_BIN"] = sys.executable
        env["PRELIMINARY_EVIDENCE_V1_VENV"] = str(runtime_root / "venv-preliminary")
        if not _stage(
            payload, name="frozen_contracts",
            argv=["bash", "tools/preliminary_evidence_v1_gate.sh"],
            cwd=worktree, env=env, timeout=1800,
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_FROZEN_GATE_FAILED"}

        try:
            env, docker_authority = _docker_env(runtime_root, worktree)
        except Exception as exc:
            return 2, {
                **payload, "verdict": "INCONCLUSIVE", "reason": "CANDIDATE_FULL_GATE_DOCKER_UNAVAILABLE",
                "error": f"{type(exc).__name__}:{_safe_tail(str(exc))}",
            }
        payload["docker_authority"] = docker_authority
        env["CAPTURE_ENGINE_VERSION"] = "V1"
        env["CAPTURE_V2_PRODUCTION_ENABLED"] = "false"
        env["PYTHONPATH"] = "backend:."
        env["RUNNER_TEMP"] = str(runtime_root / "runner-temp")
        env["PYTHON_BIN"] = sys.executable
        env["VOIP_AI_GATE_VENV"] = str(runtime_root / "voip-ai-release-gate")

        if not _stage(
            payload, name="full_software_acceptance",
            argv=["bash", "tools/voip_ai_release_gate.sh"],
            cwd=worktree, env=env, timeout=3600,
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_FULL_GATE_FAILED"}

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
        if not _stage(
            payload, name="real_offline_golden_001",
            argv=[
                str(replay_python), "tools/offline_analysis_golden_replay.py",
                "--pcap", str(_GOLDEN_PCAP), "--require-fixture",
                "--artifacts", str(artifacts), "--result", str(result_path),
            ],
            cwd=worktree, env=env, timeout=1200,
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "CANDIDATE_REAL_OFFLINE_GOLDEN_FAILED"}
        golden = json.loads(result_path.read_text(encoding="utf-8"))
        payload["offline_golden_result"] = {
            "status": golden.get("status"),
            "passed": golden.get("passed"),
            "checks_passed": golden.get("checks_passed"),
            "checks_total": golden.get("checks_total"),
        }
        if not (
            golden.get("status") == "PASS"
            and golden.get("passed") is True
            and golden.get("checks_passed") == 142
            and golden.get("checks_total") == 142
        ):
            return 1, {**payload, "verdict": "FAIL", "reason": "OFFLINE_GOLDEN_142_CONTRACT_FAILED"}

        payload["verdict"] = "PASS"
        payload["reason"] = "MASTER_FIX_CANDIDATE_PLUS_CAPTURE_V2_FULL_ACCEPTANCE_PASS"
        return 0, payload
    except subprocess.TimeoutExpired as exc:
        payload["verdict"] = "FAIL"
        payload["reason"] = "MASTER_FIX_CANDIDATE_STAGE_TIMEOUT"
        payload["error"] = str(exc.cmd)
        return 1, payload
    except Exception as exc:
        payload["verdict"] = "FAIL"
        payload["reason"] = "MASTER_FIX_CANDIDATE_REGRESSION_EXCEPTION"
        payload["error"] = f"{type(exc).__name__}:{_safe_tail(str(exc))}"
        return 1, payload
    finally:
        if worktree_added:
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root, timeout=180)
        shutil.rmtree(runtime_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated master-fix candidate plus Capture V2 regression")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args(argv)
    rc, payload = run(repo_root=args.repo_root, candidate_sha=args.candidate_sha)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

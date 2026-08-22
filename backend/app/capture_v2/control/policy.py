from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import ControlActionType, RemoteAction


class ControlPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedCommand:
    argv: list[str]
    cwd: Path
    timeout_seconds: float
    env: dict[str, str]
    result_kind: str = "gate-cli"


class ControlPolicy:
    """Fail-closed action policy. No arbitrary shell is accepted from Git."""

    GENERATED_PREFIXES = (
        "validation/control/",
        ".capture-v2-control/",
    )
    CONTROL_COMMIT_PREFIXES = ("validation/control/",)
    _PORCELAIN_PREFIX_RE = re.compile(r"^[ MADRCU?!]{1,2}\s+")
    _GOLDEN_DATE_RE = re.compile(r"^20\d{6}$")

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.backend_root = (self.repo_root / "backend").resolve()

    @staticmethod
    def _required(params: dict[str, Any], *names: str) -> None:
        missing = [name for name in names if params.get(name) in (None, "")]
        if missing:
            raise ControlPolicyError("MISSING_PARAMETERS:" + ",".join(missing))

    @staticmethod
    def _add(argv: list[str], flag: str, value: Any) -> None:
        if value is not None and value != "":
            argv.extend([flag, str(value)])

    @classmethod
    def _porcelain_path(cls, line: str) -> str:
        return cls._PORCELAIN_PREFIX_RE.sub("", line, count=1).strip().strip('"')

    def _git(self, *args: str) -> str:
        cp = subprocess.run(
            ["git", *args], cwd=self.repo_root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if cp.returncode != 0:
            raise ControlPolicyError(f"GIT_CHECK_FAILED:{' '.join(args)}:{cp.stderr.strip()}")
        return cp.stdout.strip()

    def check_safety(self, action: RemoteAction) -> dict[str, str]:
        facts: dict[str, str] = {}
        if not action.safety.require_v1_authority or not action.safety.require_v2_disabled:
            raise ControlPolicyError("SAFETY_DOWNGRADE_FORBIDDEN")
        if not action.safety.require_clean_git:
            raise ControlPolicyError("CLEAN_GIT_DOWNGRADE_FORBIDDEN")
        version = str(os.getenv("CAPTURE_ENGINE_VERSION", "V1")).upper().strip()
        enabled = str(os.getenv("CAPTURE_V2_PRODUCTION_ENABLED", "false")).lower().strip()
        facts["capture_engine_version"] = version
        facts["capture_v2_production_enabled"] = enabled
        if version != "V1":
            raise ControlPolicyError("SAFETY_V1_AUTHORITY_REQUIRED")
        if enabled not in {"", "0", "false", "no", "off"}:
            raise ControlPolicyError("SAFETY_V2_PRODUCTION_MUST_BE_DISABLED")

        head = self._git("rev-parse", "HEAD")
        facts["git_head"] = head
        if not action.safety.expected_head:
            raise ControlPolicyError("EXPECTED_HEAD_REQUIRED")
        expected = action.safety.expected_head
        facts["expected_product_head"] = expected
        if head != expected:
            anc = subprocess.run(["git", "merge-base", "--is-ancestor", expected, head],
                                 cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if anc.returncode != 0:
                raise ControlPolicyError("SAFETY_HEAD_NOT_ANCESTOR")
            changed = self._git("diff", "--name-only", f"{expected}..{head}").splitlines()
            bad = [path for path in changed if path and not any(path.startswith(prefix) for prefix in self.CONTROL_COMMIT_PREFIXES)]
            if bad:
                raise ControlPolicyError("CONTROL_COMMITS_TOUCHED_PRODUCT_CODE:" + ",".join(bad[:20]))

        if action.safety.require_clean_git:
            status = self._git("status", "--porcelain", "--untracked-files=all")
            dirty = []
            for line in status.splitlines():
                path = self._porcelain_path(line)
                if not any(path.startswith(prefix) for prefix in self.GENERATED_PREFIXES):
                    dirty.append(line)
            if dirty:
                raise ControlPolicyError("SAFETY_GIT_DIRTY:" + "|".join(dirty[:20]))
        return facts

    def prepare(self, action: RemoteAction) -> PreparedCommand | None:
        p = action.parameters
        timeout = float(p.get("timeout_seconds", 900.0))
        if timeout <= 0 or timeout > 7200:
            raise ControlPolicyError("TIMEOUT_OUT_OF_RANGE")
        env = os.environ.copy()
        env.setdefault("CAPTURE_ENGINE_VERSION", "V1")
        env.setdefault("CAPTURE_V2_PRODUCTION_ENABLED", "false")
        env.setdefault("PYTHONPATH", ".")

        if action.action_type == ControlActionType.HUMAN_STEP:
            self._required(p, "instruction", "ack_token")
            return None

        if action.action_type == ControlActionType.SOFTWARE_REGRESSION:
            tests = sorted(str(x.relative_to(self.backend_root)) for x in (self.backend_root / "tests").glob("test_capture_v2_*.py"))
            if not tests:
                raise ControlPolicyError("CAPTURE_V2_TESTS_NOT_FOUND")
            return PreparedCommand(
                argv=[sys.executable, "-m", "pytest", "-q", *tests],
                cwd=self.backend_root, timeout_seconds=timeout, env=env,
                result_kind="pytest",
            )

        base = [sys.executable, "-m", "app.capture_v2.gate_cli"]
        common_device = ["device_id", "model", "host"]

        if action.action_type == ControlActionType.GATE_LEASE_RACE:
            self._required(p, "device_id", "capture_session_a", "capture_session_b")
            argv = base + ["lease-race"]
            for key, flag in [
                ("device_id", "--device-id"), ("capture_session_a", "--capture-session-a"),
                ("capture_session_b", "--capture-session-b"), ("worker_a", "--worker-a"),
                ("worker_b", "--worker-b"), ("gate_id", "--gate-id"),
                ("profile_root", "--profile-root"), ("profile_id", "--profile-id"),
                ("object_root", "--object-root"), ("output_root", "--output-root"),
                ("repo_root", "--repo-root"),
            ]:
                self._add(argv, flag, p.get(key))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.GATE_LEASE_FENCING:
            self._required(p, "device_id", "capture_session_a", "capture_session_b")
            argv = [sys.executable, "-m", "app.capture_v2.control.r1_fencing_gate"]
            for key, flag in [
                ("device_id", "--device-id"), ("capture_session_a", "--capture-session-a"),
                ("capture_session_b", "--capture-session-b"), ("worker_a", "--worker-a"),
                ("worker_b", "--worker-b"), ("gate_id", "--gate-id"),
                ("ttl_seconds", "--ttl-seconds"), ("object_root", "--object-root"),
                ("output_root", "--output-root"), ("repo_root", "--repo-root"),
            ]:
                self._add(argv, flag, p.get(key))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type in {ControlActionType.GATE_OWNERSHIP, ControlActionType.GATE_OWNERSHIP_ADOPT,
                                  ControlActionType.GATE_SEGMENT, ControlActionType.GATE_COLLECT}:
            self._required(p, *common_device)
            cmd = {
                ControlActionType.GATE_OWNERSHIP: "ownership",
                ControlActionType.GATE_OWNERSHIP_ADOPT: "ownership-adopt",
                ControlActionType.GATE_SEGMENT: "segment",
                ControlActionType.GATE_COLLECT: "collect",
            }[action.action_type]
            argv = base + [cmd]
            for key, flag in [
                ("device_id", "--device-id"), ("model", "--model"), ("host", "--host"),
                ("port", "--port"), ("username", "--username"), ("platform_id", "--platform-id"),
                ("password_env", "--password-env"), ("profile_root", "--profile-root"),
                ("profile_id", "--profile-id"), ("object_root", "--object-root"),
                ("output_root", "--output-root"), ("repo_root", "--repo-root"),
            ]:
                self._add(argv, flag, p.get(key))
            if action.action_type == ControlActionType.GATE_OWNERSHIP:
                self._required(p, "reproduction_session_id", "worker_id")
                for key, flag in [("reproduction_session_id", "--reproduction-session-id"),
                                  ("worker_id", "--worker-id"), ("gate_id", "--gate-id"),
                                  ("state_file", "--state-file"), ("hold_seconds", "--hold-seconds")]:
                    self._add(argv, flag, p.get(key))
            elif action.action_type == ControlActionType.GATE_OWNERSHIP_ADOPT:
                self._required(p, "reproduction_session_id", "worker_id", "before_state")
                for key, flag in [("reproduction_session_id", "--reproduction-session-id"),
                                  ("worker_id", "--worker-id"), ("before_state", "--before-state"),
                                  ("gate_id", "--gate-id")]:
                    self._add(argv, flag, p.get(key))
            elif action.action_type == ControlActionType.GATE_SEGMENT:
                self._required(p, "reproduction_session_id", "worker_id")
                transport = str(p.get("transport", "sftp"))
                if transport not in {"sftp", "scp"}:
                    raise ControlPolicyError("TRANSPORT_NOT_ALLOWED")
                for key, flag in [("reproduction_session_id", "--reproduction-session-id"),
                                  ("worker_id", "--worker-id"), ("gate_id", "--gate-id"),
                                  ("duration", "--duration"), ("interval", "--interval"),
                                  ("fault_plan", "--fault-plan"), ("transport", "--transport")]:
                    value = transport if key == "transport" else p.get(key)
                    self._add(argv, flag, value)
            else:
                self._required(p, "capture_session_id", "gate_id")
                self._add(argv, "--capture-session-id", p.get("capture_session_id"))
                self._add(argv, "--gate-id", p.get("gate_id"))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.GATE_READINESS_FXS:
            self._required(p, *common_device, "reproduction_session_id", "worker_id")
            transport = str(p.get("transport", "scp"))
            if transport not in {"sftp", "scp"}:
                raise ControlPolicyError("TRANSPORT_NOT_ALLOWED")
            duration = float(p.get("duration", 90.0))
            if duration <= 0 or duration > 300:
                raise ControlPolicyError("R4_FXS_DURATION_OUT_OF_RANGE")
            argv = [sys.executable, "-m", "app.capture_v2.gate.r4_cli"]
            for key, flag in [
                ("device_id", "--device-id"), ("model", "--model"), ("host", "--host"),
                ("port", "--port"), ("username", "--username"), ("platform_id", "--platform-id"),
                ("password_env", "--password-env"), ("profile_root", "--profile-root"),
                ("profile_id", "--profile-id"), ("object_root", "--object-root"),
                ("output_root", "--output-root"), ("repo_root", "--repo-root"),
                ("reproduction_session_id", "--reproduction-session-id"),
                ("worker_id", "--worker-id"), ("gate_id", "--gate-id"),
            ]:
                self._add(argv, flag, p.get(key))
            self._add(argv, "--duration", duration)
            self._add(argv, "--transport", transport)
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.GOLDEN_ARCHIVE_RECOVER:
            self._required(p, *common_device, "platform_id", "password_env", "archive_date")
            model = str(p.get("model"))
            if model not in {"APF1250", "APF3260-M"}:
                raise ControlPolicyError("GOLDEN_ARCHIVE_MODEL_NOT_ALLOWED")
            archive_date = str(p.get("archive_date"))
            if not self._GOLDEN_DATE_RE.fullmatch(archive_date):
                raise ControlPolicyError("GOLDEN_ARCHIVE_DATE_INVALID")
            argv = [sys.executable, "-m", "app.capture_v2.gate.golden_archive_recover"]
            for key, flag in [
                ("device_id", "--device-id"), ("model", "--model"), ("host", "--host"),
                ("port", "--port"), ("username", "--username"), ("platform_id", "--platform-id"),
                ("password_env", "--password-env"), ("archive_date", "--archive-date"),
            ]:
                self._add(argv, flag, p.get(key))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.GATE_EVALUATE:
            self._required(p, "bundle", "gate_id")
            argv = base + ["evaluate", "--bundle", str(p["bundle"]), "--gate-id", str(p["gate_id"])]
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.FAULT_WORKER_SIGNAL:
            self._required(p, "pid", "signal", "confirm_owned_worker")
            sig = str(p["signal"]).lower()
            if sig not in {"pause", "resume", "term", "kill"}:
                raise ControlPolicyError("WORKER_SIGNAL_NOT_ALLOWED")
            if not bool(p.get("confirm_owned_worker")):
                raise ControlPolicyError("OWNED_WORKER_CONFIRMATION_REQUIRED")
            if sig == "kill" and not action.safety.allow_worker_kill:
                raise ControlPolicyError("WORKER_KILL_NOT_APPROVED")
            argv = base + ["fault", sig, "--pid", str(int(p["pid"]))]
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.FAULT_QUARANTINE_COPY:
            self._required(p, "path", "store_root")
            if not action.safety.allow_server_quarantine:
                raise ControlPolicyError("SERVER_QUARANTINE_NOT_APPROVED")
            argv = base + ["fault", "quarantine-copy", "--path", str(p["path"]),
                           "--store-root", str(p["store_root"])]
            self._add(argv, "--quarantine-root", p.get("quarantine_root"))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        if action.action_type == ControlActionType.FAULT_RESTORE_COPY:
            self._required(p, "token", "store_root")
            argv = base + ["fault", "restore-copy", "--token", str(p["token"]),
                           "--store-root", str(p["store_root"])]
            self._add(argv, "--quarantine-root", p.get("quarantine_root"))
            return PreparedCommand(argv, self.backend_root, timeout, env)

        raise ControlPolicyError("ACTION_NOT_REGISTERED")

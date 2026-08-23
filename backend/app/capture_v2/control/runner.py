from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_sync import GitControlSync, GitSyncError
from .policy import ControlPolicy, ControlPolicyError, PreparedCommand
from .schema import ControlActionType, ControlState, ControlStatus, RemoteAction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class RemoteValidationRunner:
    CONTROL_RELOAD_PREFIXES = (
        "backend/app/capture_v2/control/",
        "backend/app/capture_v2/control_cli.py",
    )
    _SENSITIVE_LINE_RE = re.compile(
        r"(?i)(password|passwd|secret|authorization|cookie|private[_ -]?key|api[_ -]?token|access[_ -]?token)"
    )

    def __init__(self, *, repo_root: Path, action_path: Path | None = None,
                 branch: str = "feat/capture-v2.1.1-real-gates", remote: str = "origin",
                 git_sync: bool = False, runner_id: str | None = None):
        self.repo_root = repo_root.resolve()
        self.action_path = action_path or (self.repo_root / "validation/control/next_action.json")
        self.status_path = self.repo_root / "validation/control/status.json"
        self.result_root = self.repo_root / "validation/control/results"
        self.local_root = self.repo_root / ".capture-v2-control"
        self.ledger_path = self.local_root / "ledger.json"
        self.ack_path = self.repo_root / "validation/control/human_ack.json"
        self.runner_id = runner_id or f"{socket.gethostname()}:{os.getpid()}"
        self.policy = ControlPolicy(self.repo_root)
        self.sync = GitControlSync(self.repo_root, remote=remote, branch=branch) if git_sync else None
        self.result_root.mkdir(parents=True, exist_ok=True)
        self.local_root.mkdir(parents=True, exist_ok=True)
        self._loaded_head = self._current_git_head() if self.sync else None

    def _current_git_head(self) -> str | None:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None

    def _control_source_changed(self, old_head: str, new_head: str) -> bool:
        cp = subprocess.run(
            ["git", "diff", "--name-only", f"{old_head}..{new_head}"],
            cwd=self.repo_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"CONTROL_RELOAD_DIFF_FAILED:{cp.stderr.strip()}")
        return any(
            any(path.startswith(prefix) for prefix in self.CONTROL_RELOAD_PREFIXES)
            for path in cp.stdout.splitlines() if path.strip()
        )

    def _reexec_current_process(self) -> None:
        os.execv(
            sys.executable,
            [sys.executable, "-m", "app.capture_v2.control_cli", *sys.argv[1:]],
        )

    def _maybe_reexec_after_sync(self, new_head: str) -> None:
        old_head = self._loaded_head
        if not old_head or old_head == new_head:
            self._loaded_head = new_head
            return
        if self._control_source_changed(old_head, new_head):
            self._reexec_current_process()
        self._loaded_head = new_head

    def _load_ledger(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"last_sequence": 0, "actions": {}}
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def _save_ledger(self, ledger: dict[str, Any]) -> None:
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.ledger_path)

    def _write_status(self, status: ControlStatus) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(status.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _publish_runner_error(self, exc: Exception) -> None:
        error = f"{type(exc).__name__}:{exc}"
        previous: dict[str, Any] = {}
        if self.status_path.exists():
            try:
                previous = json.loads(self.status_path.read_text(encoding="utf-8"))
            except Exception:
                previous = {}
        if previous.get("state") == "RUNNER_ERROR" and previous.get("error") == error:
            return
        payload = {
            "schema_version": "capture-v2-remote-status-v1",
            "state": "RUNNER_ERROR",
            "runner_id": self.runner_id,
            "updated_at": _now(),
            "error": error,
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not self.sync:
            return
        try:
            self.sync.commit_and_push([self.status_path], message="capture-v2-control: runner error")
        except GitSyncError as sync_exc:
            payload["git_sync_error"] = str(sync_exc)
            self.status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _parse_gate_output(stdout: str) -> tuple[str | None, dict[str, Any] | None]:
        text = stdout.strip()
        if not text:
            return None, None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(payload, dict):
            return None, payload
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            verdict = verdict.get("value")
        if verdict is None and payload.get("result") and isinstance(payload["result"], dict):
            verdict = payload["result"].get("verdict")
        return str(verdict) if verdict else None, payload

    @classmethod
    def _safe_failure_tail(cls, text: str, *, max_lines: int = 120, max_chars: int = 12000) -> str:
        """Return a bounded diagnostic tail with obviously sensitive lines removed."""
        if not text:
            return ""
        selected = text.splitlines()[-max_lines:]
        redacted = ["[REDACTED_SENSITIVE_LINE]" if cls._SENSITIVE_LINE_RE.search(line) else line
                    for line in selected]
        joined = "\n".join(redacted)
        return joined[-max_chars:]

    def _human_ack(self, action: RemoteAction) -> bool:
        if not self.ack_path.exists():
            return False
        try:
            payload = json.loads(self.ack_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return payload.get("action_id") == action.action_id and payload.get("token") == action.parameters.get("ack_token")

    def _execute(self, command: PreparedCommand) -> tuple[int, str, str]:
        cp = subprocess.run(command.argv, cwd=command.cwd, env=command.env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=command.timeout_seconds, shell=False)
        return cp.returncode, cp.stdout, cp.stderr

    def process_once(self) -> ControlStatus | None:
        if self.sync:
            synced_head = self.sync.pull_ff_only()
            self._maybe_reexec_after_sync(synced_head)
        if not self.action_path.exists():
            return None
        action = RemoteAction.load(self.action_path)
        digest = action.digest()
        ledger = self._load_ledger()
        previous = ledger.get("actions", {}).get(action.action_id)
        if previous:
            if previous.get("action_sha256") != digest:
                return self._terminal(action, digest, ControlState.REJECTED, error="ACTION_ID_REUSE_CONFLICT")
            if self.status_path.exists():
                raw = json.loads(self.status_path.read_text(encoding="utf-8"))
                if raw.get("action_id") == action.action_id:
                    return ControlStatus(action_id=raw["action_id"], sequence=int(raw["sequence"]), action_type=raw["action_type"],
                        state=ControlState(raw["state"]), runner_id=raw["runner_id"], action_sha256=raw["action_sha256"],
                        updated_at=raw["updated_at"], started_at=raw.get("started_at"), finished_at=raw.get("finished_at"),
                        return_code=raw.get("return_code"), verdict=raw.get("verdict"), result_path=raw.get("result_path"),
                        stdout_sha256=raw.get("stdout_sha256"), stderr_sha256=raw.get("stderr_sha256"),
                        error=raw.get("error"), detail=raw.get("detail") or {})
            return None
        if action.sequence <= int(ledger.get("last_sequence", 0)):
            return self._terminal(action, digest, ControlState.REJECTED, error="SEQUENCE_NOT_MONOTONIC")
        if action.expired():
            return self._terminal(action, digest, ControlState.EXPIRED, error="ACTION_EXPIRED")

        try:
            safety = self.policy.check_safety(action)
            prepared = self.policy.prepare(action)
        except (ControlPolicyError, ValueError) as exc:
            return self._terminal(action, digest, ControlState.REJECTED, error=str(exc))

        started = _now()
        status = ControlStatus(action_id=action.action_id, sequence=action.sequence, action_type=action.action_type.value,
            state=ControlState.RUNNING, runner_id=self.runner_id, action_sha256=digest,
            updated_at=started, started_at=started, detail={"safety": safety})
        self._write_status(status)

        if action.action_type == ControlActionType.HUMAN_STEP:
            if not self._human_ack(action):
                status.state = ControlState.WAITING_HUMAN
                status.updated_at = _now()
                status.detail["instruction"] = str(action.parameters["instruction"])
                status.detail["ack_command"] = (f"python -m app.capture_v2.control_cli ack --action-id {action.action_id} "
                                                f"--token {action.parameters['ack_token']}")
                self._write_status(status)
                self._maybe_push([self.status_path], action, status)
                return status
            return self._terminal(action, digest, ControlState.SUCCEEDED, verdict="HUMAN_ACKED",
                                  detail={"safety": safety, "instruction": action.parameters["instruction"]})

        try:
            rc, stdout, stderr = self._execute(prepared)
        except subprocess.TimeoutExpired as exc:
            return self._terminal(action, digest, ControlState.FAILED, error="ACTION_TIMEOUT",
                                  stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""), detail={"safety": safety})
        except Exception as exc:
            return self._terminal(action, digest, ControlState.FAILED,
                                  error=f"EXECUTION_ERROR:{type(exc).__name__}:{exc}", detail={"safety": safety})

        verdict, payload = self._parse_gate_output(stdout)
        if rc == 0:
            final = ControlState.SUCCEEDED
        elif verdict in {"INCONCLUSIVE", "DEFERRED_REAL_GATE"}:
            final = ControlState.INCONCLUSIVE
        else:
            final = ControlState.FAILED
        return self._terminal(action, digest, final, return_code=rc, verdict=verdict,
                              stdout=stdout, stderr=stderr, detail={"safety": safety, "parsed_output": payload})

    def _terminal(self, action: RemoteAction, digest: str, state: ControlState, *, error: str | None = None,
                  return_code: int | None = None, verdict: str | None = None, stdout: str = "", stderr: str = "",
                  detail: dict[str, Any] | None = None) -> ControlStatus:
        finished = _now()
        result_dir = self.result_root / action.action_id
        result_dir.mkdir(parents=True, exist_ok=True)
        local_dir = self.local_root / "logs" / action.action_id
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
        (local_dir / "stderr.log").write_text(stderr, encoding="utf-8", errors="replace")
        detail_payload = dict(detail or {})
        if state == ControlState.FAILED and action.action_type == ControlActionType.SOFTWARE_REGRESSION:
            detail_payload["failure_stdout_tail"] = self._safe_failure_tail(stdout)
            detail_payload["failure_stderr_tail"] = self._safe_failure_tail(stderr)
        result = {"schema_version": "capture-v2-remote-result-v1", "action": action.canonical_dict(),
                  "action_sha256": digest, "runner_id": self.runner_id, "state": state.value,
                  "return_code": return_code, "verdict": verdict, "error": error, "finished_at": finished,
                  "stdout_sha256": _sha(stdout), "stderr_sha256": _sha(stderr),
                  "local_stdout": str((local_dir / "stdout.log").relative_to(self.repo_root)),
                  "local_stderr": str((local_dir / "stderr.log").relative_to(self.repo_root)), "detail": detail_payload}
        result_path = result_dir / "result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        status = ControlStatus(action_id=action.action_id, sequence=action.sequence, action_type=action.action_type.value,
            state=state, runner_id=self.runner_id, action_sha256=digest, updated_at=finished,
            finished_at=finished, return_code=return_code, verdict=verdict,
            result_path=str(result_path.relative_to(self.repo_root)), stdout_sha256=result["stdout_sha256"],
            stderr_sha256=result["stderr_sha256"], error=error, detail=detail_payload)
        self._write_status(status)
        ledger = self._load_ledger()
        ledger.setdefault("actions", {})[action.action_id] = {"action_sha256": digest, "sequence": action.sequence,
            "state": state.value, "result_path": status.result_path, "finished_at": finished}
        ledger["last_sequence"] = max(int(ledger.get("last_sequence", 0)), action.sequence)
        self._save_ledger(ledger)
        self._maybe_push([result_path, self.status_path], action, status)
        return status

    def _maybe_push(self, paths: list[Path], action: RemoteAction, status: ControlStatus) -> None:
        if not self.sync:
            return
        try:
            sha = self.sync.commit_and_push(paths, message=f"capture-v2-control: {action.action_id} {status.state.value}")
            status.detail["result_commit"] = sha
            self._write_status(status)
        except GitSyncError as exc:
            status.detail["git_sync_error"] = str(exc)
            self._write_status(status)

    def run_forever(self, *, poll_seconds: float = 10.0) -> None:
        if poll_seconds < 1.0:
            raise ValueError("POLL_SECONDS_TOO_SMALL")
        while True:
            try:
                self.process_once()
            except Exception as exc:
                self._publish_runner_error(exc)
            time.sleep(poll_seconds)

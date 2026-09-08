from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.automation.gates.golden_web_config import SipRegistrationEvidence


class FusionPbxRegistrationProbeError(RuntimeError):
    pass


RegistrationCommandRunner = Callable[[tuple[str, ...], float], tuple[int | None, str]]


class FusionPbxRegistrationProbe:
    """Read-only FusionPBX/FreeSWITCH registration observer.

    The exact command pair is bound by ``current-pbx-provider-source-probe-v2``
    on the controlled runner. Raw ``fs_cli`` output is process-private: only
    return codes and exact target-identity booleans are retained in evidence.
    No PBX or DUT mutation is performed here.
    """

    COMMANDS: tuple[tuple[str, str], ...] = (
        ("show_registrations", "show registrations"),
        ("sofia_internal_reg", "sofia status profile internal reg"),
    )

    def __init__(
        self,
        *,
        fs_cli_bin: str = "fs_cli",
        poll_interval_seconds: float = 2.0,
        command_timeout_seconds: float = 8.0,
        runner: RegistrationCommandRunner | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("PBX_REGISTRATION_POLL_INTERVAL_INVALID")
        if command_timeout_seconds <= 0:
            raise ValueError("PBX_REGISTRATION_COMMAND_TIMEOUT_INVALID")
        self.fs_cli_bin = fs_cli_bin
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.command_timeout_seconds = float(command_timeout_seconds)
        self._runner = runner or self._run_command
        self._uses_default_runner = runner is None

    @staticmethod
    def _run_command(argv: tuple[str, ...], timeout_seconds: float) -> tuple[int | None, str]:
        try:
            cp = subprocess.run(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        return int(cp.returncode), cp.stdout or ""

    @staticmethod
    def _identity_observed(output: str, number: str) -> bool:
        # Match the identifier as an exact SIP-ish token, not as an IP/UUID/
        # larger extension substring. This is intentionally the same boundary
        # contract used by the controlled-runner read-only source probe.
        pattern = re.compile(
            rf"(?<![0-9A-Za-z_.+]){re.escape(number)}(?![0-9A-Za-z_.+])"
        )
        return bool(pattern.search(output or ""))

    def _observe_once(self, number: str) -> tuple[bool, dict[str, Any], tuple[str, ...]]:
        if self._uses_default_runner and shutil.which(self.fs_cli_bin) is None:
            raise FusionPbxRegistrationProbeError("FUSIONPBX_FS_CLI_REQUIRED")

        commands: dict[str, dict[str, Any]] = {}
        evidence_refs: list[str] = []
        observed = False
        for probe_name, command in self.COMMANDS:
            rc, output = self._runner(
                (self.fs_cli_bin, "-x", command),
                self.command_timeout_seconds,
            )
            hit = rc == 0 and self._identity_observed(output, number)
            commands[probe_name] = {
                "rc": rc,
                "identity_observed": bool(hit),
                "nonempty": bool(output.strip()),
            }
            evidence_refs.append(f"fusionpbx-runtime://{probe_name}/{number}")
            observed = observed or bool(hit)

        return observed, {
            "provider": "fusionpbx_fs_cli",
            "mutation": False,
            "secret_values_emitted": False,
            "identity_observed": observed,
            "commands": commands,
        }, tuple(evidence_refs)

    async def wait_registered(self, *, number: str, timeout_seconds: float) -> SipRegistrationEvidence:
        target = str(number).strip()
        if not target or not target.isascii() or not target.isdigit():
            raise FusionPbxRegistrationProbeError("PBX_NUMERIC_REGISTRATION_TARGET_REQUIRED")
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 60.0:
            raise FusionPbxRegistrationProbeError("PBX_REGISTRATION_TIMEOUT_INVALID")

        deadline = time.monotonic() + timeout
        last_details: dict[str, Any] = {}
        last_refs: tuple[str, ...] = ()
        while True:
            registered, last_details, last_refs = await asyncio.to_thread(
                self._observe_once,
                target,
            )
            if registered:
                return SipRegistrationEvidence(
                    registered=True,
                    number=target,
                    evidence_refs=last_refs,
                    source_timestamp=datetime.now(timezone.utc),
                    details=last_details,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SipRegistrationEvidence(
                    registered=False,
                    number=target,
                    evidence_refs=last_refs,
                    source_timestamp=datetime.now(timezone.utc),
                    details=last_details,
                )
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))

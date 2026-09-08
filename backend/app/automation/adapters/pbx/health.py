from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import Callable

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence


FsRunner = Callable[[tuple[str, ...], float], tuple[int | None, str]]
SocketProbe = Callable[[str, int, float], bool]


@dataclass(frozen=True)
class PbxHealthResult:
    ready: bool
    checks: dict[str, bool]
    node_key: str
    active_domain_count: int
    pool_occupied_count: int
    pool_first_available: int | None
    protected_registration: dict[str, bool]
    source_fence_version: str

    def safe_dict(self) -> dict:
        return {
            "schema_version": "pbx-health-v1",
            "ready": self.ready,
            "status": "PBX_READY" if self.ready else "PBX_NOT_READY",
            "node_key": self.node_key,
            "checks": dict(self.checks),
            "active_domain_count": self.active_domain_count,
            "extension_pool": {
                "occupied_count": self.pool_occupied_count,
                "first_available": self.pool_first_available,
            },
            "protected_registration": dict(self.protected_registration),
            "source_fence_version": self.source_fence_version,
            "mutation_executed": False,
            "secret_values_emitted": False,
        }


class PbxHealthGate:
    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        config_provider: FusionPbxConfigProvider | None = None,
        source_fence: FusionPbxSourceFence | None = None,
        fs_runner: FsRunner | None = None,
        socket_probe: SocketProbe | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.profile = profile
        self.config = config_provider or FusionPbxConfigProvider(profile)
        self.source_fence = source_fence or FusionPbxSourceFence(profile)
        self._fs_runner = fs_runner or self._run_fs
        self._socket_probe = socket_probe or self._probe_socket
        self.timeout_seconds = float(timeout_seconds)

    def _run_fs(
        self, argv: tuple[str, ...], timeout_seconds: float
    ) -> tuple[int | None, str]:
        if shutil.which(argv[0]) is None:
            return None, ""
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
    def _probe_socket(host: str, port: int, timeout_seconds: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True
        except OSError:
            return False

    @staticmethod
    def _registration_identities(output: str) -> set[str]:
        result: set[str] = set()
        for raw in (output or "").splitlines():
            line = raw.strip()
            if (
                not line
                or line.lower().startswith("reg_user,")
                or line.endswith("total.")
            ):
                continue
            first = line.split(",", 1)[0].strip()
            if re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", first):
                result.add(first)
        return result

    def check(self) -> PbxHealthResult:
        fence = self.source_fence.inspect()
        try:
            domains = self.config.discover_domains()
            identities = self.config.list_identities()
            config_readable = True
        except Exception:
            domains = ()
            identities = {}
            config_readable = False

        status_rc, status_out = self._fs_runner(
            (self.profile.fs_cli_bin, "-x", "status"), self.timeout_seconds
        )
        profile_rc, profile_out = self._fs_runner(
            (
                self.profile.fs_cli_bin,
                "-x",
                f"sofia status profile {self.profile.internal_profile}",
            ),
            self.timeout_seconds,
        )
        reg_rc, reg_out = self._fs_runner(
            (self.profile.fs_cli_bin, "-x", "show registrations"),
            self.timeout_seconds,
        )
        fs_ready = status_rc == 0 and "is ready" in status_out.lower()
        profile_text = profile_out.lower()
        profile_ready = (
            profile_rc == 0
            and bool(re.search(r"(?m)^name\s+" + re.escape(self.profile.internal_profile.lower()) + r"\s*$", profile_text))
            and f":{self.profile.internal_port}" in profile_text
            and "sip-ip" in profile_text
        )
        registrations = (
            self._registration_identities(reg_out) if reg_rc == 0 else set()
        )
        protected = {
            value: value in registrations for value in self.profile.protected_extensions
        }

        all_identities: set[str] = set()
        for values in identities.values():
            all_identities.update(values)
        occupied = {
            int(value)
            for value in all_identities
            if value.isdigit()
            and self.profile.pool_start <= int(value) <= self.profile.pool_end
        }
        first_available = next(
            (
                value
                for value in range(self.profile.pool_start, self.profile.pool_end + 1)
                if value not in occupied
            ),
            None,
        )
        pool_enumerable = config_readable and first_available is not None
        port_ready = self._socket_probe(
            self.profile.host,
            self.profile.internal_port,
            min(self.timeout_seconds, 3.0),
        )
        checks = {
            "source_fence": fence.ok,
            "fusionpbx_database": config_readable and bool(domains),
            "freeswitch_runtime": fs_ready,
            "internal_profile_running": profile_ready,
            "internal_sip_port_reachable": port_ready,
            "extension_pool_enumerable": pool_enumerable,
            "registration_observable": reg_rc == 0,
        }
        return PbxHealthResult(
            ready=all(checks.values()),
            checks=checks,
            node_key=self.profile.node_key,
            active_domain_count=len(domains),
            pool_occupied_count=len(occupied),
            pool_first_available=first_available,
            protected_registration=protected,
            source_fence_version=fence.version,
        )

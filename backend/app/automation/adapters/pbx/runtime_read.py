from __future__ import annotations

import re
import shutil
import subprocess
from typing import Callable

from app.automation.adapters.pbx.profile import FusionPbxLabProfile


RuntimeRunner = Callable[[tuple[str, ...], float], tuple[int | None, str]]
_IDENTITY_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,64}$")
_DOMAIN_RE = re.compile(r"^[0-9A-Za-z._:-]{1,255}$")


class FreeSwitchRuntimeReadError(RuntimeError):
    pass


class FreeSwitchRuntimeReadProbe:
    """Read-only FreeSWITCH directory/runtime reconcile used before ESL M4.

    `user_exists` is authoritative for exact directory visibility on the current
    lab. `list_users` remains auxiliary because this FreeSWITCH build can return
    an empty list even while exact `user_exists` is true.
    """

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        runner: RuntimeRunner | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.profile = profile
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner or self._run

    def _run(self, argv: tuple[str, ...], timeout_seconds: float) -> tuple[int | None, str]:
        if shutil.which(argv[0]) is None:
            return None, ""
        try:
            cp = subprocess.run(
                list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        return int(cp.returncode), cp.stdout or ""

    @staticmethod
    def _parse_first_field(output: str) -> set[str]:
        result: set[str] = set()
        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("-") or line.startswith("userid|") or line in {"+OK", "-ERR"}:
                continue
            first = line.split("|", 1)[0].strip()
            if _IDENTITY_RE.fullmatch(first):
                result.add(first)
        return result

    def list_users(self) -> set[str]:
        rc, output = self._runner((self.profile.fs_cli_bin, "-x", "list_users"), self.timeout_seconds)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_LIST_USERS_FAILED")
        return self._parse_first_field(output)

    def user_visible(self, identity: str, *, domain_name: str) -> bool:
        identity = str(identity).strip()
        domain_name = str(domain_name).strip()
        if not _IDENTITY_RE.fullmatch(identity) or not _DOMAIN_RE.fullmatch(domain_name):
            raise FreeSwitchRuntimeReadError("PBX_RUNTIME_IDENTITY_INVALID")
        command = f"user_exists id {identity} {domain_name}"
        rc, output = self._runner((self.profile.fs_cli_bin, "-x", command), self.timeout_seconds)
        if rc != 0:
            raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_EXISTS_FAILED")
        normalized = (output or "").strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        raise FreeSwitchRuntimeReadError("PBX_FREESWITCH_USER_EXISTS_INVALID")

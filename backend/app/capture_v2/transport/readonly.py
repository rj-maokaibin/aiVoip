from __future__ import annotations

import shlex
from dataclasses import dataclass

from app.capture_v2.errors import CaptureV2Error


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    starttime: int
    cmdline: str


class ReadOnlyDeviceTransport:
    """Side-effect-free device reads; automatic SSH retry is allowed here."""

    def __init__(self, adapter, *, retries: int = 2):
        self.adapter = adapter
        self.retries = retries

    async def run(self, command: str, *, timeout: float | None = None) -> str:
        result = await self.adapter.execute_shell(command, timeout=timeout, retries=self.retries)
        if int(result.exit_status or 0) != 0:
            raise CaptureV2Error(
                "DEVICE_READ_FAILED",
                details={"command": command, "exit_status": int(result.exit_status or 0), "stderr": result.stderr},
            )
        return result.stdout or ""

    async def read_text(self, path: str, *, missing_ok: bool = False) -> str | None:
        q = shlex.quote(path)
        result = await self.adapter.execute_shell(
            f"if [ -r {q} ]; then cat {q}; else exit 44; fi", retries=self.retries
        )
        status = int(result.exit_status or 0)
        if status == 44 and missing_ok:
            return None
        if status != 0:
            raise CaptureV2Error("DEVICE_READ_FAILED", details={"path": path, "exit_status": status})
        return (result.stdout or "").strip()

    async def boot_id(self) -> str:
        value = await self.read_text("/proc/sys/kernel/random/boot_id")
        if not value:
            raise CaptureV2Error("DUT_BOOT_ID_UNAVAILABLE")
        return value

    async def list_epoch_dirs(self) -> list[str]:
        out = await self.run(
            "for d in /tmp/aivoip_capture/epochs/*; do "
            "[ -d \"$d\" ] && printf '%s\\n' \"${d##*/}\"; done; true"
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def list_legacy_ring_dirs(self) -> list[str]:
        out = await self.run(
            "for d in /tmp/aiVoip_ring_*; do "
            "[ -d \"$d\" ] && printf '%s\\n' \"$d\"; done; true"
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def list_tcpdump_processes(self) -> list[ProcessRecord]:
        # Match by process comm (name), not cmdline substring: the scanning
        # shell's own cmdline contains the literal "tcpdump" pattern, which
        # previously caused a self-match and a false SIP_ABA_EXISTING_TCPDUMP_PRESENT.
        command = r'''for p in /proc/[0-9]*; do
  [ -r "$p/comm" ] || continue
  c=$(cat "$p/comm" 2>/dev/null)
  case "$c" in tcpdump|tshark) ;; *) continue ;; esac
  [ -r "$p/cmdline" ] || continue
  cmd=$(tr '\000' ' ' < "$p/cmdline" 2>/dev/null)
  st=$(awk '{print $22}' "$p/stat" 2>/dev/null) || continue
  printf '%s\t%s\t%s\n' "${p##*/}" "$st" "$cmd"
done'''
        out = await self.run(command)
        result: list[ProcessRecord] = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                result.append(ProcessRecord(pid=int(parts[0]), starttime=int(parts[1]), cmdline=parts[2]))
            except ValueError:
                continue
        return result

    async def process_matches(self, *, pid: int, starttime: int) -> bool:
        pid = int(pid)
        expected = shlex.quote(str(int(starttime)))
        result = await self.adapter.execute_shell(
            f"[ -r /proc/{pid}/stat ] || exit 44; "
            f"st=$(awk '{{print $22}}' /proc/{pid}/stat 2>/dev/null) || exit 44; "
            f"[ \"$st\" = {expected} ]",
            retries=self.retries,
        )
        return int(result.exit_status or 0) == 0

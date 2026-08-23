from __future__ import annotations

import re
from dataclasses import dataclass, replace


_INTERFACE_RE = re.compile(r"(?:^|\s)-(?:n)?i\s+([^\s]+)")
_OUTPUT_RE = re.compile(r"(?:^|\s)-w\s+([^\s]+)")
_EPOCH_RE = re.compile(r"/tmp/aivoip_capture/epochs/([^/]+)/")


@dataclass(frozen=True)
class ProducerIdentity:
    pid: int
    process_starttime: int
    cmdline: str
    interface: str | None
    output_path: str | None
    capture_epoch: str | None
    session_id: str | None = None
    legacy: bool = False

    @property
    def owned_by_aivoip(self) -> bool:
        path = self.output_path or ""
        return "/tmp/aivoip_capture/" in path or "/tmp/aiVoip_ring_" in path

    def with_session(self, session_id: str | None) -> "ProducerIdentity":
        return replace(self, session_id=session_id)


def parse_process_record(pid: int, starttime: int, cmdline: str) -> ProducerIdentity:
    interface_match = _INTERFACE_RE.search(cmdline)
    output_match = _OUTPUT_RE.search(cmdline)
    output = output_match.group(1) if output_match else None
    epoch_match = _EPOCH_RE.search(output or "")
    return ProducerIdentity(
        pid=int(pid),
        process_starttime=int(starttime),
        cmdline=cmdline.strip(),
        interface=interface_match.group(1) if interface_match else None,
        output_path=output,
        capture_epoch=epoch_match.group(1) if epoch_match else None,
        legacy=bool(output and "/tmp/aiVoip_ring_" in output),
    )

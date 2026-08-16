from __future__ import annotations

import json
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator

from .normalize import normalize_ek_record
from .types import NormalizedPacket


class TSharkUnavailable(RuntimeError):
    pass


class TSharkAnalysisError(RuntimeError):
    pass


class TSharkAdapter:
    """Streaming adapter around TShark's line-oriented Elastic/Kibana JSON output.

    The adapter keeps Wireshark/TShark outside the semantic domain model. It applies
    an overall wall-clock timeout and only emits normalized VOIP packets.
    """

    def __init__(self, binary: str = "tshark", timeout_seconds: int = 300, display_filter: str = "sip || sdp || rtp || rtcp"):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.display_filter = display_filter

    def ensure_available(self) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            raise TSharkUnavailable(f"TShark binary not found: {self.binary}")
        return resolved

    def version(self) -> str:
        binary = self.ensure_available()
        proc = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            raise TSharkAnalysisError(proc.stderr.strip() or "Unable to get TShark version")
        return proc.stdout.splitlines()[0].strip()

    def iter_packets(self, pcap_path: str | Path) -> Iterator[NormalizedPacket]:
        binary = self.ensure_available()
        cmd = [binary, "-n", "-l", "-r", str(pcap_path)]
        if self.display_filter:
            cmd += ["-Y", self.display_filter]
        # NOTE: do NOT pass `-j <protocols>` together with `-T ek`. On
        # Wireshark/TShark 4.4.x that combination emits only `{"filtered": ...}`
        # placeholders instead of real field values, so the normalizer would see
        # zero packets. Full EK output is streamed line-by-line and stays bounded.
        cmd += ["-T", "ek"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        assert proc.stdout is not None and proc.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        stderr_lines: list[str] = []
        deadline = time.monotonic() + self.timeout_seconds

        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    raise TSharkAnalysisError(f"TShark timed out after {self.timeout_seconds}s")
                events = selector.select(timeout=min(1.0, remaining))
                if not events:
                    if proc.poll() is not None:
                        break
                    continue
                for key, _ in events:
                    stream = key.fileobj
                    line = stream.readline()
                    if line == "":
                        selector.unregister(stream)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if key.data == "stderr":
                        stderr_lines.append(line)
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "layers" not in obj and "_source" not in obj:
                        continue
                    packet = normalize_ek_record(obj)
                    if packet is not None:
                        yield packet

            rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            if rc != 0:
                raise TSharkAnalysisError("\n".join(stderr_lines[-20:]) or f"TShark exited with code {rc}")
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise TSharkAnalysisError(f"TShark timed out after {self.timeout_seconds}s") from exc
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()

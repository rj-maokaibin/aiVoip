from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.capture_v2.errors import CaptureV2Error

if TYPE_CHECKING:
    from app.capture_v2.lease.manager import LeaseToken
else:
    LeaseToken = Any
from app.capture_v2.producer.identity import ProducerIdentity, parse_process_record
from app.capture_v2.transport.mutator import FencedDeviceMutator
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


@dataclass(frozen=True)
class ProducerExitStats:
    packets_captured: int | None
    packets_received: int | None
    packets_dropped_kernel: int | None


@dataclass(frozen=True)
class ProducerStartSpec:
    capture_epoch: str
    session_id: str
    interface: str
    segment_seconds: int = 5
    snaplen: int = 0


class ProducerManager:
    """Ownership-safe tcpdump lifecycle for B; segment ingest is added in Phase C."""

    def __init__(self, reader: ReadOnlyDeviceTransport, mutator: FencedDeviceMutator):
        self.reader = reader
        self.mutator = mutator

    async def inspect_owned(self) -> list[ProducerIdentity]:
        result: list[ProducerIdentity] = []
        for proc in await self.reader.list_tcpdump_processes():
            identity = parse_process_record(proc.pid, proc.starttime, proc.cmdline)
            if not identity.owned_by_aivoip:
                continue
            if identity.capture_epoch:
                session_id = await self.reader.read_text(
                    f"/tmp/aivoip_capture/epochs/{identity.capture_epoch}/session_id", missing_ok=True
                )
                identity = identity.with_session(session_id)
            result.append(identity)
        return result

    async def start(self, token: LeaseToken, spec: ProducerStartSpec) -> ProducerIdentity:
        if spec.segment_seconds != 5 or spec.snaplen != 0:
            raise CaptureV2Error("PRODUCER_PROFILE_INVARIANT_VIOLATION")
        existing = await self.inspect_owned()
        if existing:
            raise CaptureV2Error(
                "CAPTURE_CONFLICT",
                details={"producer_count": len(existing), "pids": [p.pid for p in existing]},
            )

        epoch_root = f"/tmp/aivoip_capture/epochs/{spec.capture_epoch}"
        active = f"{epoch_root}/active"
        spool = f"{epoch_root}/spool"
        body = f'''
EPOCH_ROOT={shlex.quote(epoch_root)}
mkdir -p {shlex.quote(active)} {shlex.quote(spool)}
printf '%s' {shlex.quote(spec.session_id)} > "$EPOCH_ROOT/session_id"
printf '%s' {shlex.quote(spec.interface)} > "$EPOCH_ROOT/interface"
printf '%s' {shlex.quote(spec.capture_epoch)} > "$EPOCH_ROOT/capture_epoch"
/sbin/start-stop-daemon -S -b -m -p "$EPOCH_ROOT/producer.pid" -x /usr/bin/tcpdump -- \
  -ni {shlex.quote(spec.interface)} -s 0 -U -G 5 \
  -w "$EPOCH_ROOT/active/capture_%Y%m%d_%H%M%S.pcap" \
  >"$EPOCH_ROOT/tcpdump.stdout" 2>"$EPOCH_ROOT/tcpdump.stderr"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
echo AIVOIP_PRODUCER_START_REQUESTED
'''
        try:
            await self.mutator.execute_fenced(token, body=body)
        except CaptureV2Error as exc:
            if exc.code != "MUTATION_RESULT_UNKNOWN":
                raise
            # Unknown transport result: inspect instead of blindly starting again.

        # BusyBox start-stop-daemon -b returns before proc metadata is always visible.
        for _ in range(20):
            matches = [p for p in await self.inspect_owned() if p.capture_epoch == spec.capture_epoch]
            all_owned = await self.inspect_owned()
            if len(all_owned) > 1:
                raise CaptureV2Error(
                    "PRODUCER_DUPLICATED",
                    details={"pids": [p.pid for p in all_owned]},
                )
            if len(matches) == 1:
                producer = matches[0]
                if producer.interface != spec.interface:
                    raise CaptureV2Error(
                        "PRODUCER_IDENTITY_MISMATCH",
                        details={"expected_interface": spec.interface, "actual_interface": producer.interface},
                    )
                if producer.session_id != spec.session_id:
                    raise CaptureV2Error(
                        "PRODUCER_IDENTITY_MISMATCH",
                        details={"expected_session": spec.session_id, "actual_session": producer.session_id},
                    )
                return producer
            await asyncio.sleep(0.1)
        raise CaptureV2Error("PRODUCER_START_FAILED", details={"capture_epoch": spec.capture_epoch})

    async def adopt(self, expected: ProducerIdentity) -> ProducerIdentity:
        if not await self.reader.process_matches(pid=expected.pid, starttime=expected.process_starttime):
            raise CaptureV2Error("PRODUCER_IDENTITY_MISMATCH", details={"pid": expected.pid})
        current = [p for p in await self.inspect_owned() if p.pid == expected.pid]
        if len(current) != 1 or current[0].process_starttime != expected.process_starttime:
            raise CaptureV2Error("PRODUCER_IDENTITY_MISMATCH", details={"pid": expected.pid})
        return current[0]

    async def read_exit_stats(self, capture_epoch: str) -> ProducerExitStats:
        text = await self.reader.read_text(
            f"/tmp/aivoip_capture/epochs/{capture_epoch}/tcpdump.stderr", missing_ok=True
        ) or ""
        def count(pattern: str) -> int | None:
            match = re.search(pattern, text, re.MULTILINE)
            return int(match.group(1)) if match else None
        return ProducerExitStats(
            packets_captured=count(r"(?m)^\s*(\d+)\s+packets captured\s*$"),
            packets_received=count(r"(?m)^\s*(\d+)\s+packets received by filter\s*$"),
            packets_dropped_kernel=count(r"(?m)^\s*(\d+)\s+packets dropped by kernel\s*$"),
        )

    async def _wait_identity_gone(
        self,
        *,
        pid: int,
        starttime: int,
        attempts: int = 20,
        interval_seconds: float = 0.1,
    ) -> bool:
        """Poll from the controller host, never with fractional BusyBox sleep on DUT."""
        for _ in range(attempts):
            if not await self.reader.process_matches(pid=pid, starttime=starttime):
                return True
            await asyncio.sleep(interval_seconds)
        return not await self.reader.process_matches(pid=pid, starttime=starttime)

    async def stop_identity(self, token: LeaseToken, producer: ProducerIdentity) -> None:
        pid = int(producer.pid)
        starttime = int(producer.process_starttime)

        term_body = f'''
PID={pid}
EXPECTED_ST={shlex.quote(str(starttime))}
if [ ! -r "/proc/$PID/stat" ]; then
  echo AIVOIP_PRODUCER_ALREADY_STOPPED
  exit 0
fi
CUR_ST=$(awk '{{print $22}}' "/proc/$PID/stat" 2>/dev/null || true)
[ "$CUR_ST" = "$EXPECTED_ST" ] || exit 74
kill "$PID" 2>/dev/null || true
echo AIVOIP_PRODUCER_TERM_REQUESTED
'''
        try:
            await self.mutator.execute_fenced(token, body=term_body)
        except CaptureV2Error as exc:
            if exc.code != "MUTATION_RESULT_UNKNOWN":
                raise
            # Observe-before-retry: a lost TERM response may still mean TERM took effect.
            if not await self.reader.process_matches(pid=pid, starttime=starttime):
                return

        if await self._wait_identity_gone(pid=pid, starttime=starttime):
            return

        kill_body = f'''
PID={pid}
EXPECTED_ST={shlex.quote(str(starttime))}
if [ ! -r "/proc/$PID/stat" ]; then
  echo AIVOIP_PRODUCER_ALREADY_STOPPED
  exit 0
fi
CUR_ST=$(awk '{{print $22}}' "/proc/$PID/stat" 2>/dev/null || true)
[ "$CUR_ST" = "$EXPECTED_ST" ] || exit 74
kill -9 "$PID" 2>/dev/null || true
echo AIVOIP_PRODUCER_KILL_REQUESTED
'''
        try:
            await self.mutator.execute_fenced(token, body=kill_body)
        except CaptureV2Error as exc:
            if exc.code != "MUTATION_RESULT_UNKNOWN":
                raise
            if not await self.reader.process_matches(pid=pid, starttime=starttime):
                return

        if await self._wait_identity_gone(pid=pid, starttime=starttime, attempts=10):
            return
        raise CaptureV2Error("PRODUCER_STOP_FAILED", details={"pid": pid})

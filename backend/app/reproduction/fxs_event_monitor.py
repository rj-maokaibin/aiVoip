from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable

from app.platforms.resolvers import resolve_aim_fxs_events_v1


# Full debug sequence required for the DUT to emit FXS event lines on the AIM PTY.
# Verified live on APF1250 2026-08-13: OFFHOOK / DTMF<d> / ONHOOK appear only after
# this full set is enabled; `de p on` / `debug p on` alone are insufficient.
FULL_DEBUG_ENABLE = [
    'debug p on',
    'debug sys debug',
    'de p on',
    'de sip de',
    'de ipc de',
    'de cm de',
    'de dsp de',
    'de sys de',
    'voip sip log-pkt on',
]

FULL_DEBUG_DISABLE = [
    'voip sip log-pkt off',
    'de sys off',
    'de dsp off',
    'de cm off',
    'de ipc off',
    'de sip off',
    'de p off',
    'debug sys off',
    'debug p off',
]

# Matches a timestamped FXS event line even when interleaved with IPC debug noise and
# ANSI color escape codes (the DUT colors D:: / C:: lines):
#  \x1b[33m2026-08-13 22:52:53.878000 [0] D:: [D]OFFHOOK
#  \x1b[m\x1b[36m2026-08-13 22:52:54.778000 [0] D:: [D]DTMF<1>
#  \x1b[m\x1b[33m2026-08-13 22:52:58.758000 [0] D:: [D]ONHOOK
# The timestamp is NOT anchored at line start because escape codes may prefix it.
_FXS_EVENT_LINE = re.compile(
    r'(?m)(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}) '
    r'\[(?P<line>\d+)\].*?(?P<event>OFFHOOK|ONHOOK|DTMF<(?P<digit>[0-9A-D#*])>)'
)

# Strip ANSI SGR escape sequences so event lines can be matched reliably.
_ANSI = re.compile(r'\x1b\[[0-9;]*m')

# Read a chunk of raw AIM PTY output (bytes -> str), or None on EOF.
AimChunkReader = Callable[[], str | None]
AimCommandWriter = Callable[[str], None]
RelativeClock = Callable[[], int]  # monotonic ms


@dataclass(frozen=True)
class FxsEvent:
    timestamp: str
    line: int
    event: str
    digit: str | None = None

    @property
    def is_hook(self) -> bool:
        return self.event in {'OFFHOOK', 'ONHOOK'}


@dataclass
class FxsEventMonitor:
    """Transport-injected monitor that turns the DUT's full-debug AIM event stream
    into structured OFFHOOK/DTMF/ONHOOK events.

    Production wires ``read_aim_chunk`` to the persistent AIM PTY via AsyncSSH; tests
    inject a canned stream. This keeps the event semantics identical to the
    ``AIM_FXS_EVENT_V1`` resolver while hiding the transport.
    """

    read_aim_chunk: AimChunkReader
    write_aim: AimCommandWriter
    relative_ms: RelativeClock | None = None
    event_hook: Callable[[FxsEvent], None] | None = None

    _buffer: str = field(default='', init=False)
    _started: bool = field(default=False, init=False)

    def enable_debug(self) -> None:
        for cmd in FULL_DEBUG_ENABLE:
            self.write_aim(cmd)

    def disable_debug(self) -> None:
        for cmd in FULL_DEBUG_DISABLE:
            self.write_aim(cmd)

    def start(self, *, enable_debug: bool = True) -> None:
        """Begin consuming the stream; no-op if already started.

        ``enable_debug=False`` is used by the real platform after its arm phase has
        already issued FULL_DEBUG_ENABLE, so the monitor does not re-write debug
        commands (which would duplicate AIM output on the same PTY).
        """
        if self._started:
            return
        self._started = True
        self._buffer = ''
        if enable_debug:
            self.enable_debug()

    def stop(self) -> None:
        self.disable_debug()
        self._started = False

    def poll(self) -> list[FxsEvent]:
        """Read available chunks and return newly parsed FXS events.

        Suitable for a synchronous transport. For async transports prefer ``feed`` +
        ``drain`` so chunks are handed in without a queue round-trip.
        """
        if not self._started:
            return []
        chunk = self.read_aim_chunk()
        if chunk is None:
            return []
        return self.feed(chunk)

    def feed(self, chunk: str) -> list[FxsEvent]:
        """Hand a raw AIM PTY chunk to the monitor and return newly parsed events.

        Thread/event-loop safe: the caller reads the stream (e.g. ``await stream.read``)
        and feeds each chunk directly; no internal queue is involved.
        """
        if not self._started:
            return []
        self._buffer += _ANSI.sub('', chunk)
        return self.drain()

    def drain(self) -> list[FxsEvent]:
        """Parse and return any complete FXS events buffered so far."""
        return self._drain()

    def _drain(self) -> list[FxsEvent]:
        events: list[FxsEvent] = []
        for m in _FXS_EVENT_LINE.finditer(self._buffer):
            digit = m.group('digit')
            ev = FxsEvent(
                timestamp=m.group('timestamp'),
                line=int(m.group('line')),
                event=m.group('event').split('<')[0],
                digit=digit,
            )
            events.append(ev)
            if self.event_hook is not None:
                self.event_hook(ev)
        # Keep only the trailing partial line (no event line start yet).
        last = self._buffer.rfind('\n')
        self._buffer = self._buffer[last + 1:] if last >= 0 else self._buffer
        return events

    def parse_full_output(self, output: str) -> list[FxsEvent]:
        """Parse a complete captured transcript (used by the resolver-equivalent path)."""
        parsed = resolve_aim_fxs_events_v1(output)
        return [
            FxsEvent(timestamp=p['timestamp'], line=p['line'], event=p['event'], digit=p.get('digit'))
            for p in parsed
        ]

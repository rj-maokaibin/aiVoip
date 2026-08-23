from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from app.capture_v2.segment.models import RemoteSegmentIdentity


@dataclass(frozen=True)
class SealedRemoteSegment:
    segment_seq: int
    identity: RemoteSegmentIdentity


_LINE = re.compile(r"^(?P<seq>\d+)\t(?P<path>[^\t]+)\t(?P<inode>\d+)\t(?P<size>\d+)\t(?P<mtime>\d+)$")


class SegmentSealer:
    def __init__(self, reader, mutator):
        self.reader = reader
        self.mutator = mutator

    @staticmethod
    def _body(*, capture_epoch: str, producer_pid: int, producer_starttime: int) -> str:
        epoch = shlex.quote(capture_epoch)
        return f'''ROOT=/tmp/aivoip_capture/epochs/{epoch}
ACTIVE="$ROOT/active"
SPOOL="$ROOT/spool"
SEQ="$ROOT/segment_seq"
mkdir -p "$ACTIVE" "$SPOOL"
[ -r /proc/{int(producer_pid)}/stat ] || exit 74
curst=$(awk '{{print $22}}' /proc/{int(producer_pid)}/stat 2>/dev/null || true)
[ "$curst" = {shlex.quote(str(int(producer_starttime)))} ] || exit 74
next=$(cat "$SEQ" 2>/dev/null || echo 1)
case "$next" in ''|*[!0-9]*) echo AIVOIP_SEGMENT_SEQ_CORRUPT; exit 79 ;; esac
for f in "$ACTIVE"/*.pcap; do
  [ -f "$f" ] || continue
  open=0
  for fd in /proc/{int(producer_pid)}/fd/*; do
    [ -L "$fd" ] || continue
    target=$(readlink "$fd" 2>/dev/null || true)
    [ "$target" = "$f" ] && open=1 && break
  done
  [ "$open" -eq 0 ] || continue
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  inode=$(stat -c %i "$f" 2>/dev/null || echo 0)
  mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
  [ "$size" -ge 24 ] || continue
  base=${{f##*/}}
  dst=$(printf '%s/seg_%012d__%s' "$SPOOL" "$next" "$base")
  mv "$f" "$dst" || exit 80
  inode2=$(stat -c %i "$dst" 2>/dev/null || echo 0)
  size2=$(stat -c %s "$dst" 2>/dev/null || echo 0)
  [ "$inode2" = "$inode" ] || exit 81
  [ "$size2" = "$size" ] || exit 81
  printf '%s\t%s\t%s\t%s\t%s\n' "$next" "$dst" "$inode2" "$size2" "$mtime"
  next=$((next + 1))
done
tmp="$SEQ.tmp.$$"; printf '%s' "$next" > "$tmp" && mv "$tmp" "$SEQ"
'''

    @staticmethod
    def _parse(out: str) -> tuple[SealedRemoteSegment, ...]:
        result = []
        for raw in out.splitlines():
            m = _LINE.match(raw.strip())
            if not m:
                continue
            result.append(SealedRemoteSegment(
                segment_seq=int(m.group("seq")),
                identity=RemoteSegmentIdentity(
                    remote_path=m.group("path"), inode=int(m.group("inode")),
                    size=int(m.group("size")), mtime_epoch=int(m.group("mtime")),
                ),
            ))
        return tuple(result)

    @staticmethod
    def _after_stop_body(*, capture_epoch: str, producer_pid: int, producer_starttime: int) -> str:
        epoch = shlex.quote(capture_epoch)
        return f'''ROOT=/tmp/aivoip_capture/epochs/{epoch}
ACTIVE="$ROOT/active"
SPOOL="$ROOT/spool"
SEQ="$ROOT/segment_seq"
mkdir -p "$ACTIVE" "$SPOOL"
# Final sealing is legal only after the exact old producer identity is gone. A
# reused PID with a different starttime is harmless; the old identity no longer
# owns an fd in this epoch.
if [ -r /proc/{int(producer_pid)}/stat ]; then
  curst=$(awk '{{print $22}}' /proc/{int(producer_pid)}/stat 2>/dev/null || true)
  [ "$curst" != {shlex.quote(str(int(producer_starttime)))} ] || exit 82
fi
next=$(cat "$SEQ" 2>/dev/null || echo 1)
case "$next" in ''|*[!0-9]*) echo AIVOIP_SEGMENT_SEQ_CORRUPT; exit 79 ;; esac
for f in "$ACTIVE"/*.pcap; do
  [ -f "$f" ] || continue
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  inode=$(stat -c %i "$f" 2>/dev/null || echo 0)
  mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
  [ "$size" -ge 24 ] || continue
  base=${{f##*/}}
  dst=$(printf '%s/seg_%012d__%s' "$SPOOL" "$next" "$base")
  mv "$f" "$dst" || exit 80
  inode2=$(stat -c %i "$dst" 2>/dev/null || echo 0)
  size2=$(stat -c %s "$dst" 2>/dev/null || echo 0)
  [ "$inode2" = "$inode" ] || exit 81
  [ "$size2" = "$size" ] || exit 81
  printf '%s\t%s\t%s\t%s\t%s\n' "$next" "$dst" "$inode2" "$size2" "$mtime"
  next=$((next + 1))
done
tmp="$SEQ.tmp.$$"; printf '%s' "$next" > "$tmp" && mv "$tmp" "$SEQ"
'''

    async def seal_closed(self, token, *, capture_epoch: str, producer_pid: int,
                          producer_starttime: int) -> tuple[SealedRemoteSegment, ...]:
        out = await self.mutator.execute_fenced(token, body=self._body(
            capture_epoch=capture_epoch, producer_pid=producer_pid,
            producer_starttime=producer_starttime,
        ))
        return self._parse(out)

    async def seal_all_after_stop(self, token, *, capture_epoch: str, producer_pid: int,
                                  producer_starttime: int) -> tuple[SealedRemoteSegment, ...]:
        out = await self.mutator.execute_fenced(token, body=self._after_stop_body(
            capture_epoch=capture_epoch, producer_pid=producer_pid,
            producer_starttime=producer_starttime,
        ))
        return self._parse(out)

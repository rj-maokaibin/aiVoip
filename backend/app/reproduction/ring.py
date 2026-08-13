from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class RingSegment:
    segment_id: str
    start_ms: int
    end_ms: int
    path: str
    size_bytes: int = 0
    frozen: bool = False


class SegmentedRingBuffer:
    """Metadata-level segmented ring used by the Collector.

    Phase C core intentionally does not implement device tcpdump I/O. The ring contract is
    deterministic and testable; EC-02/Collector integration later supplies real segment files.
    """
    def __init__(self, *, pretrigger_seconds: int, segment_seconds: int):
        self.pretrigger_ms=int(pretrigger_seconds*1000)
        self.segment_ms=int(segment_seconds*1000)
        self._segments: list[RingSegment]=[]
        self.preserve_mode=False
        self.freeze_anchor_ms: int|None=None

    @property
    def segments(self) -> tuple[RingSegment,...]:
        return tuple(self._segments)

    def append(self, segment: RingSegment) -> list[RingSegment]:
        if segment.end_ms < segment.start_ms:
            raise ValueError('SEGMENT_TIME_INVALID')
        self._segments.append(segment)
        evicted=[]
        if not self.preserve_mode:
            cutoff=segment.end_ms-self.pretrigger_ms
            keep=[]
            for item in self._segments:
                if item.end_ms < cutoff and not item.frozen:
                    evicted.append(item)
                else:
                    keep.append(item)
            self._segments=keep
        return evicted

    def freeze(self, anchor_ms: int) -> tuple[RingSegment,...]:
        self.freeze_anchor_ms=int(anchor_ms)
        cutoff=self.freeze_anchor_ms-self.pretrigger_ms
        for item in self._segments:
            if item.end_ms >= cutoff:
                item.frozen=True
        self.preserve_mode=True
        return tuple(x for x in self._segments if x.frozen)

    def manifest(self) -> dict:
        return {
            'pretrigger_ms':self.pretrigger_ms,
            'segment_ms':self.segment_ms,
            'preserve_mode':self.preserve_mode,
            'freeze_anchor_ms':self.freeze_anchor_ms,
            'segments':[asdict(x) for x in self._segments],
        }

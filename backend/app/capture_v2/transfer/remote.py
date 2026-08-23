from __future__ import annotations

import shlex

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.segment.models import RemoteSegmentIdentity


class RemoteSegmentInspector:
    def __init__(self, reader):
        self.reader = reader

    async def stat(self, path: str) -> RemoteSegmentIdentity:
        q = shlex.quote(path)
        out = await self.reader.run(
            f'[ -f {q} ] || exit 44; '
            f'p={q}; i=$(stat -c %i "$p") || exit 45; s=$(stat -c %s "$p") || exit 45; '
            f'm=$(stat -c %Y "$p") || exit 45; printf "%s\\t%s\\t%s\\t%s\\n" "$p" "$i" "$s" "$m"'
        )
        parts = out.strip().split("\t")
        if len(parts) != 4:
            raise CaptureV2Error("REMOTE_SEGMENT_STAT_PARSE_FAILED", details={"output": out[-256:]})
        return RemoteSegmentIdentity(parts[0], int(parts[1]), int(parts[2]), int(parts[3]))

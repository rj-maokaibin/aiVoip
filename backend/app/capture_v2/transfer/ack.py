from __future__ import annotations

import shlex

from app.capture_v2.segment.models import RemoteSegmentIdentity


class SegmentAcknowledger:
    def __init__(self, mutator):
        self.mutator = mutator

    async def delete_remote(self, token, identity: RemoteSegmentIdentity) -> None:
        p = shlex.quote(identity.remote_path)
        body = f'''[ -f {p} ] || exit 0
inode=$(stat -c %i {p} 2>/dev/null || echo 0)
size=$(stat -c %s {p} 2>/dev/null || echo 0)
[ "$inode" = {shlex.quote(str(identity.inode))} ] || exit 74
[ "$size" = {shlex.quote(str(identity.size))} ] || exit 74
rm -f -- {p} || exit 82
[ ! -e {p} ] || exit 82
'''
        await self.mutator.execute_fenced(token, body=body)

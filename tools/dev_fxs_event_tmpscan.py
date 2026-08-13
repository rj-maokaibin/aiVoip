"""Locate FXS event log: enable `debug p on` and scan ALL /tmp regular files for
size/content changes during the user's off-hook/dial/on-hook, then grep event
markers. This catches files not in the known candidate list.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


async def shell(adapter, cmd, timeout=8):
    r = await adapter.execute_shell(cmd, timeout=timeout)
    return r.stdout or r.stderr


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        # Snapshot /tmp regular files (path:size).
        snap = await shell(adapter,
            "find /tmp -maxdepth 3 -type f -printf '%p|%s\\n' 2>/dev/null", timeout=8)
        before = {}
        for line in snap.splitlines():
            if '|' in line:
                p, s = line.rsplit('|', 1)
                before[p] = s

        # Enable debug p on (also de p on for good measure).
        process.stdin.write('debug p on\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        process.stdin.write('de p on\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== debug p on + de p on enabled ===')
        print(f'>>> LISTENING 50s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK <<<')

        await asyncio.sleep(50)

        # Snapshot again and find changed/new files.
        snap2 = await shell(adapter,
            "find /tmp -maxdepth 3 -type f -printf '%p|%s\\n' 2>/dev/null", timeout=8)
        after = {}
        for line in snap2.splitlines():
            if '|' in line:
                p, s = line.rsplit('|', 1)
                after[p] = s

        changed = []
        for p, s in after.items():
            if before.get(p) != s:
                changed.append((p, before.get(p, 'NEW'), s))
        print('=== changed/new files under /tmp ===')
        for p, old, new in sorted(changed):
            print(f'  {p}: {old} -> {new}')

        # Grep event markers in changed files.
        print('=== FXS markers in changed files ===')
        found = False
        for p, old, new in changed:
            out = await shell(adapter,
                f"grep -aE 'OFFHOOK|ONHOOK|DTMF|hook' {p} 2>/dev/null; true", timeout=6)
            if out.strip():
                found = True
                print(f'--- {p} ---')
                for line in out.splitlines()[-15:]:
                    if any(k in line for k in ('OFFHOOK', 'ONHOOK', 'DTMF', 'hook')):
                        print('   ', line)
        if not found:
            print('(no FXS markers found in changed files)')

        # Restore.
        process.stdin.write('de p off\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        process.stdin.write('debug p off\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== debug off restored ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

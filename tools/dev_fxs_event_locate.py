"""Locate the FXS event log by enabling debug and watching ALL candidate logs.
Enables de p on, then tails candidate log files + syslog + scans /tmp for new
event lines while the user performs off-hook/dial/on-hook. Reports which file
contains OFFHOOK/DTMF/ONHOOK. Restores de p off.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt

# Candidate logs where FXS events may be written.
CANDIDATES = [
    '/tmp/voip_log.txt',
    '/tmp/networkapi/networkvoip.log',
    '/tmp/voip_wd_log',
    '/tmp/voip_ipc_cli_log.txt',
    '/tmp/aimThreadId.log',
]


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

        # Baseline sizes (regular files only). `wc -c < file` prints one number.
        sizes = {}
        for p in CANDIDATES:
            out = await shell(adapter, f"wc -c < {p}", timeout=5)
            sizes[p] = int(out.strip() or 0)
        print('baseline sizes:', {Path(p).name: s for p, s in sizes.items() if s})

        # Enable event debug.
        process.stdin.write('de p on\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== de p on enabled ===')
        print(f'>>> LISTENING 60s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK <<<')

        hits = {}
        start = asyncio.get_event_loop().time()
        try:
            while asyncio.get_event_loop().time() - start < 60:
                for p in CANDIDATES:
                    out = await shell(adapter,
                        f"tail -c +{sizes[p] + 1} {p} 2>/dev/null | grep -aE 'OFFHOOK|ONHOOK|DTMF|hook'; true",
                        timeout=5)
                    if out.strip():
                        hits.setdefault(p, []).append(out)
                        new_out = await shell(adapter, f"wc -c < {p}", timeout=5)
                        new_size = int(new_out.strip() or 0)
                        if new_size > sizes[p]:
                            sizes[p] = new_size
                await asyncio.sleep(3)
        except Exception as exc:
            print('loop error:', type(exc).__name__)

        print('=== FILES containing FXS events ===')
        if hits:
            for f, lines in hits.items():
                print(f'FILE: {f}')
                for block in lines[-3:]:
                    for l in block.splitlines():
                        if any(k in l for k in ('OFFHOOK', 'ONHOOK', 'DTMF', 'hook')):
                            print('   ', l)
        else:
            print('(no file captured OFFHOOK/DTMF/ONHOOK)')

        # Restore.
        process.stdin.write('de p off\n')
        try:
            await read_until_prompt(stream, 'AIM> ', 5)
            print('=== de p off restored ===')
        except Exception:
            print('(de p off restore issue)')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

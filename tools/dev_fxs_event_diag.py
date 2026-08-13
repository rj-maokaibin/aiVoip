"""Diagnose where event debug output actually goes after `debug p on`.
Checks logread, /tmp new files, dmesg, and voip log after enabling debug and
(optionally) triggering a simulate call. Restores debug off.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


async def shell(adapter, label, cmd):
    r = await adapter.execute_shell(cmd)
    print(f'=== {label} ===')
    print((r.stdout or r.stderr)[:2500])
    print()


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        async def aim(cmd, prompt='AIM> ', timeout=6):
            process.stdin.write(cmd + '\n')
            out = await read_until_prompt(stream, prompt, timeout)
            return out.rsplit(prompt, 1)[0]

        # snapshot /tmp before
        await shell(adapter, 'tmp before', 'ls -la /tmp | head -40')
        await aim('debug p on')
        await asyncio.sleep(3)
        # what changed in /tmp + logread grep
        await shell(adapter, 'tmp after', 'ls -la /tmp | head -40')
        await shell(adapter, 'logread grep hook/dtmf',
                    'logread 2>/dev/null | grep -iE "OFFHOOK|ONHOOK|DTMF|hook" | tail -30 || echo "(none)"')
        await shell(adapter, 'dmesg grep fxs',
                    'dmesg 2>/dev/null | grep -iE "fxs|hook|dtmf|offhook|onhook" | tail -20 || echo "(none)"')
        await shell(adapter, 'voip_log tail', 'tail -40 /tmp/voip_log.txt 2>/dev/null')
        await aim('debug p off')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

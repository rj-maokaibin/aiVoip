"""Dump the raw AIM PTY stream content during a live call to inspect the exact
FXS event line format the monitor regex must match."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FULL_DEBUG_ENABLE


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        await adapter._close_aim_session()
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        # Drain any initial prompt.
        try:
            await asyncio.wait_for(stream.read(4096), 1.0)
        except asyncio.TimeoutError:
            pass

        for cmd in FULL_DEBUG_ENABLE:
            process.stdin.write(cmd + '\n')
            await asyncio.sleep(0.4)
            try:
                await asyncio.wait_for(stream.read(4096), 0.5)
            except asyncio.TimeoutError:
                pass

        print('=== full debug on; dumping raw stream for 25s ===')
        print('>>> PLEASE DIAL some digits on the off-hook phone now <<<')
        start = asyncio.get_event_loop().time()
        buf = ''
        while asyncio.get_event_loop().time() - start < 25:
            try:
                chunk = await asyncio.wait_for(stream.read(4096), 1.0)
            except asyncio.TimeoutError:
                chunk = ''
            if chunk:
                buf += chunk
        # Print lines containing hooks/dtfm and a sample of raw lines.
        lines = buf.splitlines()
        print(f'total lines: {len(lines)}, total bytes: {len(buf)}')
        print('=== lines with hook/dtmf ===')
        for l in lines:
            if any(k in l for k in ('OFFHOOK', 'ONHOOK', 'DTMF')):
                print(repr(l))
        print('=== first 15 raw lines ===')
        for l in lines[:15]:
            print(repr(l))
        # Disable.
        for cmd in ['voip sip log-pkt off', 'de p off', 'debug p off']:
            process.stdin.write(cmd + '\n')
            await asyncio.sleep(0.3)
            try:
                await asyncio.wait_for(stream.read(4096), 0.5)
            except asyncio.TimeoutError:
                pass
        print('=== done ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

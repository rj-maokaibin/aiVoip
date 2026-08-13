"""Diagnostic: verify the full debug sequence actually takes effect on a fresh AIM session.
Closes any stale AIM session, opens a new one, enables debug one command at a time while
printing each response, then confirms IPC debug output is flowing. Ends by disabling debug.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE


async def read_avail(stream, seconds=1.5):
    buf = ''
    try:
        while True:
            chunk = await asyncio.wait_for(stream.read(4096), seconds)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 6000:
                break
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    return buf


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # Fresh AIM session (discard any stale PTY from prior runs).
        await adapter._close_aim_session()
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        # Drain anything already buffered.
        await read_avail(stream, 1.0)

        for cmd in FULL_DEBUG_ENABLE:
            process.stdin.write(cmd + '\n')
            out = await read_avail(stream, 1.5)
            snippet = ' | '.join(out.splitlines()[:3])[:300]
            print(f'CMD: {cmd}')
            print(f'  resp: {snippet if snippet else "(no output)"}')

        # Now read a window to confirm IPC/FXS debug output is flowing.
        print('=== reading 5s of live stream (no phone action needed) ===')
        buf = ''
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < 5:
            buf += await read_avail(stream, 1.0)
        print(f'bytes in 5s: {len(buf)}')
        lines = buf.splitlines()[:20]
        for l in lines:
            print('  ', l[:180])
        has_debug = any('IPC' in l or 'Message' in l or '[D]' in l for l in buf.splitlines())
        print('debug output flowing:', has_debug)

        # Disable debug.
        for cmd in FULL_DEBUG_DISABLE:
            process.stdin.write(cmd + '\n')
            await read_avail(stream, 1.0)
        print('=== debug disabled ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

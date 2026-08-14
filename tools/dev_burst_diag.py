"""Simulate monitor.start(): send all FULL_DEBUG_ENABLE commands back-to-back, then
listen. Determines whether the burst send (not individual sends) breaks the AIM session,
and whether FXS events appear (requires the engineer to operate the phone).
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FULL_DEBUG_ENABLE, FULL_DEBUG_DISABLE, _ANSI

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'
WATCH = 25


async def main():
    from app.integrations.credentials import get_credential_provider
    provider = get_credential_provider()
    password = await provider.get_password(sn=SN, ip=IP)
    username = provider.resolve_username(ip=IP, fallback='root')

    adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
    await adapter.connect()
    print('SSH connected')
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        def write(cmd: str):
            process.stdin.write(cmd + '\n')

        # Simulate monitor.start(): burst send, no sleep between.
        print('--- burst send FULL_DEBUG_ENABLE (like monitor.start) ---')
        try:
            for cmd in FULL_DEBUG_ENABLE:
                write(cmd)
            print('burst sent, waiting 2s...')
            await asyncio.sleep(2)
            # drain any echo
            try:
                chunk = await asyncio.wait_for(stream.read(8192), 3)
                print('post-burst drain:', repr(chunk[-200:]))
            except asyncio.TimeoutError:
                print('post-burst: no drain output')
        except BrokenPipeError:
            print('!!! BROKEN PIPE right after burst — AIM exited on burst send')
            return

        # Check channel alive
        try:
            write('show version')
            await asyncio.sleep(1)
            chunk = await asyncio.wait_for(stream.read(4096), 2)
            print('AIM alive check (show version):', repr(chunk[-120:]))
        except BrokenPipeError:
            print('!!! BROKEN PIPE on show version — AIM exited')
            return
        except asyncio.TimeoutError:
            print('AIM alive check: no output (channel alive?)')

        # Now listen for FXS events (engineer operates phone)
        print(f'=== LISTENING {WATCH}s. DO NOW: OFF-HOOK -> dial -> ON-HOOK ===')
        start = time.monotonic()
        total = 0
        events = []
        while time.monotonic() - start < WATCH:
            try:
                chunk = await asyncio.wait_for(stream.read(8192), 1.0)
            except asyncio.TimeoutError:
                chunk = ''
            except BrokenPipeError:
                print('!!! BROKEN PIPE during listen — AIM exited')
                break
            if chunk:
                total += len(chunk)
                clean = _ANSI.sub('', chunk)
                # look for OFFHOOK/DTMF/ONHOOK
                for kw in ('OFFHOOK', 'ONHOOK', 'DTMF<'):
                    if kw in clean:
                        print(f'  >>> FOUND {kw} in chunk, bytes={total}')
                        print(f'      tail={clean[-300:]!r}')
                # print a sample line if any
                for line in clean.splitlines()[-3:]:
                    if '[' in line:
                        print('      line:', line[:120])
            if any('OFFHOOK' in _ANSI.sub('', e[1]) for e in []) is False and 'OFFHOOK' in clean and 'ONHOOK' in clean:
                pass
        print(f'=== listen done: total {total} bytes ===')

        # restore debug
        try:
            for cmd in FULL_DEBUG_DISABLE:
                write(cmd)
        except Exception as e:
            print('disable err:', type(e).__name__)
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    asyncio.run(main())

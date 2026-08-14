"""Minimal diagnostic: connect to the real DUT, enable full debug, listen on the AIM
PTY for a short window, and print raw stream bytes + parsed FXS events. No orchestrator,
no cleanup complexity — isolates whether the DUT actually emits OFFHOOK/DTMF/ONHOOK.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FxsEventMonitor

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'
WATCH = 20


async def main():
    from app.integrations.credentials import get_credential_provider
    provider = get_credential_provider()
    password = await provider.get_password(sn=SN, ip=IP)
    username = provider.resolve_username(ip=IP, fallback='root')

    adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
    await adapter.connect()
    print('SSH connected', username, IP, PORT)
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        loop = asyncio.get_event_loop()

        def write_aim(cmd: str):
            process.stdin.write(cmd + '\n')

        monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=write_aim)
        monitor.start()  # sends FULL_DEBUG_ENABLE

        print(f'=== LISTENING {WATCH}s. DO NOW: OFF-HOOK -> dial -> ON-HOOK ===')
        start = time.monotonic()
        total = 0
        events = []
        while time.monotonic() - start < WATCH:
            try:
                chunk = await asyncio.wait_for(stream.read(4096), 1.0)
            except asyncio.TimeoutError:
                chunk = ''
            if chunk:
                total += len(chunk)
                print(f'  [chunk +{len(chunk)}] raw={chunk[-200:]!r}')
                evs = monitor.feed(chunk)
                for e in evs:
                    events.append(e)
                    print(f'  >>> EVENT {e.timestamp} [line {e.line}] {e.event}' + (f'<{e.digit}>' if e.digit else ''))
            if any(e.event == 'OFFHOOK' for e in events) and any(e.event == 'ONHOOK' for e in events):
                print('  (full cycle captured, stopping early)')
                break
        monitor.stop()
        print(f'=== read {total} bytes, {len(events)} events ===')
        for e in events:
            print(f'  {e.timestamp} [line {e.line}] {e.event}' + (f'<{e.digit}>' if e.digit else ''))
        if not events:
            print('  (NO EVENTS — debug may not be emitting, or format differs)')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

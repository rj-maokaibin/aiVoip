"""EC-02 end-to-end: real DUT FXS events driven through FxsEventMonitor.
Connects via AsyncSSH, enables the full debug set, polls the AIM PTY with the
monitor while the user performs off-hook/dial/on-hook, and prints the parsed
events. Restores debug off.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt
from app.reproduction.fxs_event_monitor import FxsEventMonitor


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        loop = asyncio.get_event_loop()

        def write_aim(cmd: str):
            process.stdin.write(cmd + '\n')

        def hook(e):
            print(f'  >>> {e.timestamp} [line {e.line}] {e.event}' + (f'<{e.digit}>' if e.digit else ''))

        monitor = FxsEventMonitor(
            read_aim_chunk=lambda: None,  # not used; async path uses feed()
            write_aim=write_aim,
            event_hook=hook,
        )
        monitor.start()
        print('=== full debug enabled; monitor started ===')
        print(f'>>> LISTENING 60s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK <<<')

        events = []
        start = loop.time()
        total_chunks = 0
        total_bytes = 0
        try:
            while loop.time() - start < 60:
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), 1.0)
                except asyncio.TimeoutError:
                    chunk = ''
                if chunk:
                    total_chunks += 1
                    total_bytes += len(chunk)
                    evs = monitor.feed(chunk)
                    events.extend(evs)
                    has_off = any(e.event == 'OFFHOOK' for e in events)
                    has_on = any(e.event == 'ONHOOK' for e in events)
                    if has_off and has_on:
                        break
        finally:
            monitor.stop()

        print(f'=== read {total_chunks} chunks / {total_bytes} bytes during window ===')

        print('=== captured FXS events ===')
        if events:
            for e in events:
                print(f'  {e.timestamp} [line {e.line}] {e.event}' + (f'<{e.digit}>' if e.digit else ''))
            print(f'=== {len(events)} events, cycle complete: {any(e.event=="OFFHOOK" for e in events) and any(e.event=="ONHOOK" for e in events)} ===')
        else:
            print('(no events captured)')

        # Confirm root prompt still works.
        process.stdin.write('sys show bind-if\n')
        out = await read_until_prompt(stream, 'AIM> ', 5)
        print('root prompt OK:', 'Bind Interface' in out)
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

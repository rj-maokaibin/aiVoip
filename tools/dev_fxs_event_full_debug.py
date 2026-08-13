"""EC-02 FXS event stream validation with the FULL debug sequence enabled.
Enables the full debug set, listens on the persistent AIM PTY stream while the
user performs off-hook/dial/on-hook, extracts OFFHOOK/DTMF/ONHOOK lines with the
AIM_FXS_EVENT_V1 parser, then restores debug off.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt
from app.platforms.resolvers import resolve_aim_fxs_events_v1

FULL_DEBUG = [
    'debug p on',
    'debug sys debug',
    'de p on',
    'de sip de',
    'de ipc de',
    'de cm de',
    'de dsp de',
    'de sys de',
    'voip sip log-pkt on',
]
FULL_DEBUG_OFF = [
    'voip sip log-pkt off',
    'de sys off',
    'de dsp off',
    'de cm off',
    'de ipc off',
    'de sip off',
    'de p off',
    'debug sys off',
    'debug p off',
]

_EVENT = re.compile(r'(?m)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}) \[(\d+)\].*?(OFFHOOK|ONHOOK|DTMF<[0-9A-D#*]>)')


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        # Enable full debug set.
        for cmd in FULL_DEBUG:
            process.stdin.write(cmd + '\n')
            try:
                await read_until_prompt(stream, 'AIM> ', 4)
            except Exception:
                pass
        print('=== FULL debug enabled ===')
        print(f'>>> LISTENING 60s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK <<<')

        buf = ''
        events = []
        start = asyncio.get_event_loop().time()
        try:
            while asyncio.get_event_loop().time() - start < 60:
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), 2.0)
                    if chunk:
                        buf += chunk
                        # Extract event lines from buffer.
                        for m in _EVENT.finditer(buf):
                            events.append({
                                'timestamp': m.group(1),
                                'line': int(m.group(2)),
                                'event': m.group(3).split('<')[0],
                                'digit': m.group(3)[5:-1] if m.group(3).startswith('DTMF<') else None,
                            })
                        buf = ''
                        # Stop once we have a full OFFHOOK -> (DTMF) -> ONHOOK cycle.
                        has_off = any(e['event'] == 'OFFHOOK' for e in events)
                        has_on = any(e['event'] == 'ONHOOK' for e in events)
                        if has_off and has_on:
                            break
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            print('loop error:', type(exc).__name__)

        print('=== FXS events captured ===')
        if events:
            for e in events:
                print(f"  {e['timestamp']} [line {e['line']}] {e['event']}" + (f"<{e['digit']}>" if e['digit'] else ''))
        else:
            print('(no FXS events captured)')

        # Validate with the official parser on reconstructed lines.
        if events:
            text = '\n'.join(
                f"{e['timestamp']} [{e['line']}] D:: [D]{e['event']}" + (f"<{e['digit']}>" if e['digit'] else '')
                for e in events
            )
            try:
                parsed = resolve_aim_fxs_events_v1(text)
                print(f'=== AIM_FXS_EVENT_V1 parser: {len(parsed)} events parsed ===')
                for p in parsed:
                    print('  ', p)
            except Exception as exc:
                print('parser error:', exc)

        # Restore debug off.
        for cmd in FULL_DEBUG_OFF:
            process.stdin.write(cmd + '\n')
            try:
                await read_until_prompt(stream, 'AIM> ', 4)
            except Exception:
                pass
        print('=== debug off restored ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

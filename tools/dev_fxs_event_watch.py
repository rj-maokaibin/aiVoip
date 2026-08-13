"""Enable event debug and watch ALL plausible log outputs for FXS events.
Opens debug p on, then polls voip_log.txt, wd log, dmesg, and the AIM stream
while the user performs off-hook/dial/on-hook. Reports any OFFHOOK/DTMF/ONHOOK
lines. Disables debug afterwards.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


LOG_PATHS = ['/tmp/voip_log.txt', '/tmp/voip_wd_log']


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

        # Capture baseline sizes of log files.
        sizes = {}
        for p in LOG_PATHS:
            r = await adapter.execute_shell(f'wc -c < {p} 2>/dev/null || echo 0')
            sizes[p] = int((r.stdout or '0').strip() or 0)

        print('=== debug p on ===')
        await aim('debug p on')
        LISTEN = 25
        print(f'=== watching {LISTEN}s. PLEASE: OFF-HOOK -> dial -> ON-HOOK now ===')

        collected = []
        deadline = asyncio.get_event_loop().time() + LISTEN
        try:
            while asyncio.get_event_loop().time() < deadline:
                # Read AIM stream non-blocking.
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), 1.0)
                    if chunk:
                        collected.append(('AIM', chunk))
                except asyncio.TimeoutError:
                    pass
                # Poll log file deltas.
                for p in LOG_PATHS:
                    r = await adapter.execute_shell(
                        f'tail -c +{sizes[p] + 1} {p} 2>/dev/null | grep -E "OFFHOOK|ONHOOK|DTMF|hook" || true')
                    if (r.stdout or '').strip():
                        collected.append((p, r.stdout))
                await asyncio.sleep(2)
        except Exception as exc:
            print('loop error:', type(exc).__name__)

        print('=== captured event lines ===')
        seen = False
        for src, text in collected:
            for l in text.splitlines():
                if any(k in l for k in ('OFFHOOK', 'ONHOOK', 'DTMF', 'hook')):
                    print(f'[{src}] {l}')
                    seen = True
        if not seen:
            print('(no OFFHOOK/DTMF/ONHOOK lines captured anywhere)')
        print('=== debug p off ===')
        await aim('debug p off')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

"""Enable event debug and listen on the persistent AIM stream for FXS event lines.
Opens debug p on, then reads the AIM PTY stream for a window; reports any lines
that match OFFHOOK/DTMF/ONHOOK. Disables debug afterwards. The user should
perform an off-hook/dial/on-hook while this is listening.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt
from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count  # noqa: F401 (keep import parity)


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

        print('=== enabling event debug (debug p on) ===')
        print(repr((await aim('debug p on')).strip()))
        # Drain any buffered output, then listen for LISTEN_SECONDS.
        LISTEN = 20
        print(f'=== listening {LISTEN}s on AIM stream for FXS events... ===')
        print('>>> Please perform: OFF-HOOK -> dial digits -> ON-HOOK now <<<')
        buf = ''
        deadline = asyncio.get_event_loop().time() + LISTEN
        try:
            while asyncio.get_event_loop().time() < deadline:
                chunk = await asyncio.wait_for(stream.read(4096), LISTEN + 1)
                if not chunk:
                    break
                buf += chunk
                # stop early if we see both offhook and onhook
                if 'OFFHOOK' in buf and 'ONHOOK' in buf:
                    break
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            print('read error:', type(exc).__name__)
        print('=== captured during listen ===')
        lines = buf.splitlines()
        events = [l for l in lines if any(k in l for k in ('OFFHOOK', 'ONHOOK', 'DTMF', 'hook'))]
        for l in events[-40:]:
            print('EVT>', l)
        if not events:
            print('(no OFFHOOK/DTMF/ONHOOK lines captured)')
        print('=== captured total bytes:', len(buf), '| lines:', len(lines), '===')
        # Disable debug.
        print('=== disabling (debug p off) ===')
        print(repr((await aim('debug p off')).strip()))
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

"""FXS event watch using `de p on` + syslog(logread) + log files + AIM PTY.
Records a DTMF counter baseline, enables event debug, listens ~60s while the
user performs off-hook/dial/on-hook, then reports counter delta and any event
lines. Restores debug off.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt

LOG_PATHS = ['/tmp/voip_log.txt', '/tmp/voip_wd_log', '/tmp/voip_ipc_cli_log.txt']


async def shell(adapter, cmd, timeout=8):
    r = await adapter.execute_shell(cmd, timeout=timeout)
    return r.stdout or r.stderr


async def fxs_dtmf_count(adapter, process, stream):
    """Return current DTMF Detect Cnt via FXS submode show information."""
    try:
        process.stdin.write('voip fxs 1\n')
        await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
        process.stdin.write('show information\n')
        buf = ''
        try:
            while True:
                chunk = await asyncio.wait_for(stream.read(4096), 3)
                if not chunk:
                    break
                buf += chunk
                if 'AIM(fxs/1)> ' in buf:
                    break
        except asyncio.TimeoutError:
            pass
        process.stdin.write('exit\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        for line in buf.splitlines():
            s = line.strip()
            if s.startswith('DTMF Detect Cnt'):
                return int(s.split(':')[1].strip())
        return -1
    except Exception:
        return -1


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        base = await fxs_dtmf_count(adapter, process, stream)
        print('DTMF Detect Cnt baseline:', base)

        sizes = {}
        for p in LOG_PATHS:
            sizes[p] = int((await shell(adapter, f'wc -c < {p} 2>/dev/null || echo 0')).strip() or 0)

        # Enable event debug (de p on) - known reversible.
        process.stdin.write('de p on\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== de p on enabled ===')
        print(f'>>> LISTENING 60s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK <<<')

        collected = []
        start = asyncio.get_event_loop().time()
        try:
            while asyncio.get_event_loop().time() - start < 60:
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), 1.0)
                    if chunk:
                        collected.append(('AIM', chunk))
                except asyncio.TimeoutError:
                    pass
                for p in LOG_PATHS:
                    out = await shell(adapter,
                        f'tail -c +{sizes[p] + 1} {p} 2>/dev/null | grep -E "OFFHOOK|ONHOOK|DTMF|hook|fx" || true', timeout=5)
                    if out.strip():
                        collected.append((p, out))
                # syslog
                out = await shell(adapter,
                    'logread 2>/dev/null | grep -iE "OFFHOOK|ONHOOK|DTMF|fxs|hook" | tail -20 || true', timeout=5)
                if out.strip():
                    collected.append(('LOGREAD', out))
                await asyncio.sleep(2)
        except Exception as exc:
            print('loop error:', type(exc).__name__)

        after = await fxs_dtmf_count(adapter, process, stream)
        print('DTMF Detect Cnt after :', after)
        print('DTMF delta            :', (after - base) if base >= 0 and after >= 0 else 'n/a')

        print('=== captured event lines ===')
        seen = False
        for src, text in collected:
            for l in text.splitlines():
                if any(k in l for k in ('OFFHOOK', 'ONHOOK', 'DTMF', 'hook', 'fx')):
                    print(f'[{src}] {l}')
                    seen = True
        if not seen:
            print('(no event lines captured)')

        # Restore: de p off
        process.stdin.write('de p off\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== de p off restored ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

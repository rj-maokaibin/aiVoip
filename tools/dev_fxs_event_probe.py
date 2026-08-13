"""Probe where FXS hook/dtmf events are emitted on the live DUT.
Checks AIM CLI help and likely log files for OFFHOOK/ONHOOK/DTMF event lines.
Read-only.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout


async def shell(adapter, label, cmd):
    r = await adapter.execute_shell(cmd)
    print(f'=== {label} ===')
    print((r.stdout or r.stderr)[:2000])
    print()


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # 1. Grep known VOIP log files for FXS event markers.
        await shell(adapter, 'voip_log grep FXS/event',
                    "grep -inE 'OFFHOOK|ONHOOK|DTMF|hook' /tmp/voip_log.txt 2>/dev/null | tail -20 || echo '(no matches / file missing)'")
        await shell(adapter, 'networkvoip log grep event',
                    "ls -la /tmp/voip* /tmp/*voip* 2>/dev/null | head -20")
        await shell(adapter, 'ralink pcm grep',
                    "cat /proc/ralink_pcm/cfg 2>/dev/null | head -30 || echo '(no ralink pcm)'")
        # 2. AIM: try `voip fxs 1` then `?` or `help` to find event/log commands.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        # In FXS submode, list available commands.
        try:
            process.stdin.write('voip fxs 1\n')
            await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
            process.stdin.write('?\n')
            out = await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
            print('=== FXS submode `?` ===')
            print(out.rsplit('AIM(fxs/1)> ', 1)[0][-2000:])
            process.stdin.write('exit\n')
            await read_until_prompt(stream, adapter.aim_prompt, 5)
        except PromptTimeout:
            print('(fxs submode ? not available)')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

"""Probe /tmp/voip_wd_log for FXS event lines and inspect `simulate` usage.
Read-only except the simulate help query which is non-mutating.
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
    print((r.stdout or r.stderr)[:2500])
    print()


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        await shell(adapter, 'voip_wd_log FXS events',
                    "grep -inE 'OFFHOOK|ONHOOK|DTMF|hook' /tmp/voip_wd_log 2>/dev/null | tail -20 || echo '(no matches)'")
        await shell(adapter, 'voip_wd_log tail', 'tail -30 /tmp/voip_wd_log 2>/dev/null')
        # FXS submode: query simulate help.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        try:
            process.stdin.write('voip fxs 1\n')
            await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
            process.stdin.write('simulate ?\n')
            out = await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
            print('=== simulate help ===')
            print(out.rsplit('AIM(fxs/1)> ', 1)[0][-1500:])
            process.stdin.write('exit\n')
            await read_until_prompt(stream, adapter.aim_prompt, 5)
        except PromptTimeout:
            print('(simulate help unavailable)')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

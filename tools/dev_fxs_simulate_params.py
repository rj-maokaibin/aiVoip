"""Probe FXS simulate sub-command parameters and current state. Read-only."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


async def aim_sub(process, stream, cmd, prompt='AIM(fxs/1)> ', timeout=5):
    process.stdin.write(cmd + '\n')
    out = await read_until_prompt(stream, prompt, timeout)
    return out.rsplit(prompt, 1)[0]


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        await aim_sub(process, stream, 'voip fxs 1')
        for label, cmd in [
            ('simulate state ?', 'simulate state ?'),
            ('simulate type ?', 'simulate type ?'),
            ('simulate show', 'simulate show'),
            ('simulate state', 'simulate state'),
            ('simulate called ?', 'simulate called ?'),
        ]:
            try:
                out = await aim_sub(process, stream, cmd)
                print(f'=== {label} ===')
                print(out.strip() or '(empty)')
                print()
            except Exception as exc:
                print(f'=== {label}: {type(exc).__name__} ===')
        await aim_sub(process, stream, 'exit', prompt='AIM> ')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

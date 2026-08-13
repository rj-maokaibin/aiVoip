"""Inspect IPC log and AIM root-mode commands for a realtime event source.
Read-only.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


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
        await shell(adapter, 'voip_ipc_cli_log', 'cat /tmp/voip_ipc_cli_log.txt 2>/dev/null | tail -30')
        await shell(adapter, 'list /tmp/voip dir', 'ls -la /tmp/voip/ 2>/dev/null | head -30')
        # AIM root mode `?` to find event/log commands.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        process.stdin.write('?\n')
        out = await read_until_prompt(stream, adapter.aim_prompt, 5)
        print('=== AIM root `?` (first 2000) ===')
        print(out.rsplit('AIM> ', 1)[0][-2000:])
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

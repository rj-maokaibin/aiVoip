"""Re-verify the AIM readonly commands with full raw output (not truncated)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        for label, cmd in [
            ('GET_AIM_SIP_CONFIG', 'voip sip regc show config RC1'),
            ('GET_SIP_REGISTER', 'voip sip regc show running RC1'),
            ('GET_DSP_RUNNING', 'voip dsp show running 1'),
        ]:
            process.stdin.write(cmd + '\n')
            out = await read_until_prompt(stream, adapter.aim_prompt, 8)
            clean = out.rsplit(adapter.aim_prompt, 1)[0]
            # Drop the echoed command line.
            lines = clean.splitlines()
            body = [l for l in lines if cmd not in l]
            print(f'=== {label} ===')
            print('\n'.join(body).strip() or '(EMPTY OUTPUT)')
            print()
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

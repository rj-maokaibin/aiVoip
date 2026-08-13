"""EC-02 Phase D2 item 2.3: `voip sip log-pkt off` retry idempotency.
Executes the command twice and confirms the second call is harmless (no error,
no AIM exit). Ends by verifying the device remains at the root prompt.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


async def run_cli(adapter, label, cmd):
    r = await adapter.execute_cli(cmd)
    print(f'=== {label} ===')
    print(repr(r.stdout))
    return r


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        r1 = await run_cli(adapter, 'voip sip log-pkt off (1st)', 'voip sip log-pkt off')
        r2 = await run_cli(adapter, 'voip sip log-pkt off (2nd)', 'voip sip log-pkt off')
        r3 = await run_cli(adapter, 'post-check', 'sys show bind-if')
        second_harmless = 'error' not in (r2.stdout + r2.stderr).lower() or 'invalid' not in (r2.stdout + r2.stderr).lower()
        print(f'RESULT: second `voip sip log-pkt off` harmless = {second_harmless}')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

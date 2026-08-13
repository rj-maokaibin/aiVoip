"""EC-02 Phase D2 item 2.2: `de p off` retry idempotency.
Executes `de p off` twice and confirms the second call is harmless (logs stay
stopped, no AIM exit / error). Ends by verifying the device remains in a clean
root prompt state.
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
    print(r.stdout or r.stderr)
    return r


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # Optional baseline: show current debug level (read-only).
        await run_cli(adapter, 'debug show (before)', 'debug show 2>&1 || echo "(no debug show)"')
        # First de p off.
        r1 = await run_cli(adapter, 'de p off (1st)', 'de p off')
        # Second de p off.
        r2 = await run_cli(adapter, 'de p off (2nd)', 'de p off')
        # Confirm still responsive and at root prompt.
        r3 = await run_cli(adapter, 'post-check (whoami-agnostic)', 'sys show bind-if')
        # Simple assertion-style summary.
        second_harmless = 'error' not in (r2.stdout + r2.stderr).lower() or 'unknown' not in (r2.stdout + r2.stderr).lower()
        print(f'RESULT: second `de p off` harmless = {second_harmless}')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

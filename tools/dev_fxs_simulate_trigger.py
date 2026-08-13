"""Probe the exact `simulate` command syntax to trigger a call simulation event,
while watching the AIM stream + log files for FXS event lines. Deterministic:
the event is triggered by this script, not by the user's timing.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout


async def shell(adapter, cmd, timeout=6):
    r = await adapter.execute_shell(cmd, timeout=timeout)
    return r.stdout or r.stderr


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        # Enter FXS submode.
        process.stdin.write('voip fxs 1\n')
        await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)

        # Try to trigger a called-ring simulation (deterministic event).
        cmds = [
            'simulate type called',
            'simulate called 1234',
            'simulate called-ring enable',
            'simulate start',
        ]
        for cmd in cmds:
            process.stdin.write(cmd + '\n')
            try:
                out = await read_until_prompt(stream, 'AIM(fxs/1)> ', 4)
                print(f'=== {cmd} ===')
                print((out.rsplit('AIM(fxs/1)> ', 1)[0]).strip() or '(no output)')
            except PromptTimeout:
                print(f'=== {cmd}: (prompt not returned) ===')

        # Now check whether ring/hook events appeared; keep AIM stream open a moment.
        await asyncio.sleep(3)
        try:
            chunk = await asyncio.wait_for(stream.read(4096), 2)
            if chunk:
                print('=== AIM stream during simulate ===')
                print(chunk[-1500:])
        except asyncio.TimeoutError:
            pass

        # Stop simulation.
        process.stdin.write('simulate stop\n')
        await read_until_prompt(stream, 'AIM(fxs/1)> ', 4)
        process.stdin.write('exit\n')
        await read_until_prompt(stream, 'AIM> ', 5)
        print('=== simulate stopped, back to root ===')

        # Check DTMF/hook counters now.
        print('=== post-check FXS state ===')
        await shell(adapter, "echo done", timeout=3)
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

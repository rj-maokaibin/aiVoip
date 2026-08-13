"""EC-02 Phase D2 item 2.4: FXS AIM submode prompt contract.
Enters `voip fxs 1` and records whether the AIM prompt changes to a sub-mode
prompt, attempts `show information`, then exits back to the root prompt.
Uses raw PTY interaction via the persistent AIM session.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # Open AIM session manually to observe the raw prompt transitions.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        process.stdin.write('voip fxs 1\n')
        try:
            out = await read_until_prompt(stream, adapter.aim_prompt, 6)
            clean = out.rsplit(adapter.aim_prompt, 1)[0]
            print('=== after `voip fxs 1` ===')
            print(repr(clean[-600:]))
            # If we returned to root prompt, submode either doesn't exist or is flat.
            # Try `show information` at whatever prompt is active.
        except PromptTimeout:
            print('=== after `voip fxs 1` ===')
            print('(submode prompt changed or command hung - capturing raw)')
            # Try to see what prompt is showing; send newline to flush.
            process.stdin.write('\n')
        # Attempt show information.
        process.stdin.write('show information\n')
        try:
            out = await read_until_prompt(stream, adapter.aim_prompt, 5)
            clean = out.rsplit(adapter.aim_prompt, 1)[0]
            print('=== after `show information` ===')
            print(repr(clean[-800:]))
        except PromptTimeout:
            print('=== `show information` timed out (no root prompt returned) ===')
        # Exit back to root.
        process.stdin.write('exit\n')
        try:
            await read_until_prompt(stream, adapter.aim_prompt, 5)
            print('=== `exit` returned to root prompt OK ===')
        except PromptTimeout:
            print('=== `exit` did not return to root prompt (submode remained) ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

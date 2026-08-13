"""EC-02 Phase D2 item 2.4b: capture the exact FXS submode prompt string.
Enters `voip fxs 1`, sends a bare newline to flush the sub-mode prompt, reads
the raw output to identify the prompt text, then exits back to root.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout


async def read_raw(stream, seconds):
    """Best-effort read of whatever is available on the stream."""
    buf = ''
    try:
        while True:
            chunk = await asyncio.wait_for(stream.read(4096), seconds)
            if not chunk:
                break
            buf += chunk
            if '\n' in buf and len(buf) > 40:
                break
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    return buf


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        # Enter FXS 1 submode.
        process.stdin.write('voip fxs 1\n')
        await asyncio.sleep(1.5)
        raw = await read_raw(stream, 2)
        print('=== raw after `voip fxs 1` ===')
        print(repr(raw))
        # Flush the submode prompt with a newline and capture.
        process.stdin.write('\n')
        await asyncio.sleep(1.0)
        raw2 = await read_raw(stream, 2)
        print('=== raw after newline flush ===')
        print(repr(raw2))
        # Try `show information` again and capture raw.
        process.stdin.write('show information\n')
        await asyncio.sleep(1.5)
        raw3 = await read_raw(stream, 2)
        print('=== raw after `show information` ===')
        print(repr(raw3))
        # Exit back to root.
        process.stdin.write('exit\n')
        try:
            await read_until_prompt(stream, adapter.aim_prompt, 5)
            print('=== exited to root prompt OK ===')
        except PromptTimeout:
            print('=== exit did not return to root prompt ===')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

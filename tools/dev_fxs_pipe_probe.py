"""Read the VOIP SIP IPC named pipes to see if FXS events flow through them.
Non-blocking read attempt with timeout; restores nothing (read-only).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


async def shell(adapter, label, cmd, timeout=6):
    r = await adapter.execute_shell(cmd, timeout=timeout)
    print(f'=== {label} ===')
    print((r.stdout or r.stderr)[:3000])
    print()


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # Check pipe metadata and try a non-blocking read.
        await shell(adapter, 'pipe stat',
                    'ls -la /tmp/VOIP_MODULE_V1_SIP_CN_pipe_main_in /tmp/VOIP_MODULE_V1_SIP_CN_pipe_main_out 2>&1')
        # Try reading the _out pipe non-blocking (timeout guards against block).
        await shell(adapter, 'pipe _out read (nonblock, 3s)',
                    'timeout -t 3 cat /tmp/VOIP_MODULE_V1_SIP_CN_pipe_main_out 2>&1 | head -30 || echo "(timeout/empty)"', timeout=8)
        # Try the _in pipe too (may be write-side; read may block - short timeout).
        await shell(adapter, 'pipe _in read (nonblock, 2s)',
                    'timeout -t 2 cat /tmp/VOIP_MODULE_V1_SIP_CN_pipe_main_in 2>&1 | head -10 || echo "(timeout/empty)"', timeout=6)
        # Also check syslogd / other FIFOs.
        await shell(adapter, 'fifo list', 'find /tmp -type p 2>/dev/null | head -30')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

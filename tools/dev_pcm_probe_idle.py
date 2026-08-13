"""EC-02 Phase D2 item 2.1: non-mutating PCM activity probe.
Read-only tcpdump capture on the voice interface for UDP 40000/50000.
No OFF/ON commands are executed. Confirms whether a non-mutating way to
determine PCM forwarding activity is observable (packet observation).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        # BusyBox: timeout -t N (not GNU timeout). Capture up to 1 packet for 5s.
        for port in (40000, 50000):
            cmd = f"timeout -t 5 tcpdump -ni br-lan_400 -c 1 'udp port {port}' 2>&1"
            r = await adapter.execute_shell(cmd)
            print(f'=== probe UDP {port} ===')
            print(r.stdout or r.stderr)
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

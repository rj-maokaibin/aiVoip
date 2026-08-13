"""EC-02 active-call PCM RX validation (raw PTY).
During a live call:
  ON -> probe 40000 (expect packets) -> single OFF -> probe (expect 0).
Uses the persistent AIM PTY directly with the root prompt for dsp commands,
so no sub-mode wrapping is involved.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout
from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count

ROOT = 'AIM>'


async def aim(process, stream, cmd, prompt=ROOT, timeout=8):
    process.stdin.write(cmd + '\n')
    out = await read_until_prompt(stream, prompt, timeout)
    return out.rsplit(prompt, 1)[0]


async def probe(adapter, port, seconds=5):
    cmd = f"timeout -t {seconds} tcpdump -ni br-lan_400 -c 1 'udp port {port}' 2>&1"
    r = await adapter.execute_shell(cmd)
    out = r.stdout or r.stderr
    try:
        return parse_tcpdump_packet_count(out)
    except ValueError:
        print('RAW tcpdump output:', out)
        return -1


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        # ON
        out1 = await aim(process, stream, 'voip dsp diag set 192.168.3.200 40000 1 pcm_rx on')
        print('=== pcm_rx on ==='); print(repr(out1.strip()))

        # probe active
        n1 = await probe(adapter, 40000)
        print(f'=== probe 40000 after ON: {n1} packets ===')

        # single OFF
        out2 = await aim(process, stream, 'voip dsp diag set 192.168.3.200 40000 1 pcm_rx off')
        print('=== pcm_rx off (single) ==='); print(repr(out2.strip()))

        # probe quiet
        n2 = await probe(adapter, 40000)
        print(f'=== probe 40000 after OFF: {n2} packets ===')

        # root intact
        try:
            await aim(process, stream, 'sys show bind-if', timeout=5)
            print('=== root prompt intact ===')
        except PromptTimeout:
            print('=== root prompt LOST after OFF ===')

        print(f'RESULT: ON->active={n1>0}, single OFF->quiet={n2==0}')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

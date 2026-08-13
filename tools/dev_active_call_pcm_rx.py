"""EC-02 active-call PCM RX validation.
During a live call:
  1. confirm FXS/SIP call state,
  2. `pcm_rx on` (mirror media to UDP 40000),
  3. probe 40000 -> expect packets (active),
  4. single `pcm_rx off`,
  5. probe 40000 -> expect 0 packets (quiet),
  6. confirm AIM session still at root prompt.
Single OFF only; never repeats the non-idempotent OFF.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout
from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count


async def cli(adapter, cmd):
    r = await adapter.execute_cli(cmd)
    return r.stdout or r.stderr


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
        # 1. Confirm call state via FXS snapshot.
        out = await cli(adapter, 'voip fxs 1')
        await cli(adapter, 'show information')
        # Re-read to get the FXS output; simpler: re-enter and read raw.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        process.stdin.write('voip fxs 1\n')
        try:
            await read_until_prompt(stream, 'AIM(fxs/1)> ', 5)
            process.stdin.write('show information\n')
            buf = ''
            try:
                while True:
                    chunk = await asyncio.wait_for(stream.read(4096), 3)
                    if not chunk:
                        break
                    buf += chunk
                    if 'AIM(fxs/1)> ' in buf:
                        break
            except asyncio.TimeoutError:
                pass
            print('=== FXS state during call ===')
            for line in buf.splitlines():
                s = line.strip()
                if s.startswith(('Hook State', 'State ', 'Channel Id', 'DTMF')):
                    print(' ', s)
        except PromptTimeout:
            print('(fxs submode not reached)')
        process.stdin.write('exit\n')
        try:
            await read_until_prompt(stream, adapter.aim_prompt, 5)
        except PromptTimeout:
            print('(exit to root failed)')

        # 2. pcm_rx on
        print('=== pcm_rx on ===')
        print(await cli(adapter, 'voip dsp diag set 192.168.3.200 40000 1 pcm_rx on'))

        # 3. probe active
        n1 = await probe(adapter, 40000)
        print(f'=== probe 40000 after ON: {n1} packets ===')

        # 4. single off
        print('=== pcm_rx off (single) ===')
        print(await cli(adapter, 'voip dsp diag set 192.168.3.200 40000 1 pcm_rx off'))

        # 5. probe quiet
        n2 = await probe(adapter, 40000)
        print(f'=== probe 40000 after OFF: {n2} packets ===')

        # 6. root prompt intact
        try:
            r = await adapter.execute_cli('sys show bind-if')
            print('=== root prompt intact (bind-if OK) ===')
        except Exception as exc:
            print('=== root prompt BROKEN:', exc, '===')

        print(f'RESULT: ON->active={n1>0}, single OFF->quiet={n2==0}')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

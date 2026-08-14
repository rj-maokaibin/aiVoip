"""Step-by-step: find which FULL_DEBUG_ENABLE command kills the AIM session on this DUT."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FULL_DEBUG_ENABLE, FULL_DEBUG_DISABLE

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'


async def main():
    from app.integrations.credentials import get_credential_provider
    provider = get_credential_provider()
    password = await provider.get_password(sn=SN, ip=IP)
    username = provider.resolve_username(ip=IP, fallback='root')

    adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
    await adapter.connect()
    print('SSH connected')
    try:
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        print('AIM session opened, prompt ready')

        def write(cmd: str):
            process.stdin.write(cmd + '\n')

        # 1) baseline: confirm AIM alive
        write('sys show version')
        await asyncio.sleep(1)
        try:
            chunk = await asyncio.wait_for(stream.read(4096), 2)
            print('baseline sys show version ->', repr(chunk[:150]))
        except asyncio.TimeoutError:
            print('baseline: no output (maybe prompt not in stream)')

        # 2) send each debug command, check channel alive after each
        for i, cmd in enumerate(FULL_DEBUG_ENABLE):
            try:
                write(cmd)
                await asyncio.sleep(1.2)
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), 1.5)
                    snippet = chunk[-120:] if chunk else '(empty)'
                    print(f'[{i}] {cmd!r}: channel alive, output={snippet!r}')
                except asyncio.TimeoutError:
                    print(f'[{i}] {cmd!r}: channel alive, no output')
            except BrokenPipeError:
                print(f'[{i}] {cmd!r}: *** BROKEN PIPE — AIM session exited ***')
                # try to reopen
                break
            except Exception as e:
                print(f'[{i}] {cmd!r}: {type(e).__name__}: {e}')

        # 3) try disable to restore
        for cmd in FULL_DEBUG_DISABLE:
            try:
                write(cmd)
                await asyncio.sleep(0.4)
            except Exception as e:
                print('disable', cmd, '->', type(e).__name__)
                break
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    asyncio.run(main())

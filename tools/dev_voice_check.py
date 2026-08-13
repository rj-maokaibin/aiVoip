"""One-off EC-02 voice runtime read-only check.
Reads credentials from DEV_* env vars (never printed), connects with legacy KEX,
and confirms the Voice Gateway / Voice VLAN / interface resolvers used by the
RUIJIE_VOIP_AIM_V1 platform contract.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


async def run_shell(adapter, label, cmd):
    r = await adapter.execute_shell(cmd)
    print(f'=== {label} ===')
    print(r.stdout or r.stderr)


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        await run_shell(adapter, 'voipServInfo (voice gateway)', 'dev_config get -m voipServInfo')
        await run_shell(adapter, 'voice_vlan', 'dev_config get -m voice_vlan')
        await run_shell(adapter, 'brctl show', 'brctl show 2>/dev/null | head -20')
        await run_shell(adapter, 'PCM counters', 'voip dsp diag show 2>/dev/null || echo "(dsp diag show unavailable)"')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

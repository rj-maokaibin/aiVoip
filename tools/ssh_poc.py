"""Standalone SSH + AIM PoC.
Run inside backend container or a Python environment with backend requirements installed.
Password can come from Mock or real CredentialProvider; it is never printed.
"""
import argparse, asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.core.config import settings
from app.integrations.credentials import get_credential_provider
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter

async def main(args):
    pwd = await get_credential_provider().get_password(sn=args.sn, ip=args.ip)
    dev = AsyncSSHDeviceAdapter(ip=args.ip, port=args.port, username=settings.ssh_username, password=pwd)
    await dev.connect()
    try:
        shell = await dev.execute_shell("uname -a; ps | grep '[a]imd.s' || true")
        print('=== SHELL ===')
        print(shell.stdout)
        aim = await dev.execute_cli("sys show bind-if")
        print('=== AIM ===')
        print(aim.stdout)
    finally:
        await dev.disconnect()

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--ip', required=True); p.add_argument('--port', type=int, default=22); p.add_argument('--sn', required=True)
    asyncio.run(main(p.parse_args()))

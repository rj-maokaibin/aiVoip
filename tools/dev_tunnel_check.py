"""EWEB tunnel health check for on-site / remote diagnosis.

The customer-site tunnel is manually opened and expires ~3h (stok/stamp/pass),
so before/after a reproduction run an operator can run this to learn clearly
whether the tunnel is alive, expired, or mis-configured - instead of guessing
from a reproduction failure.

Prints one of:
  TUNNEL_OK             - SSH + AIM both work; reproduction can proceed.
  TUNNEL_UNREACHABLE    - connect timeout/refused: likely expired; re-open the
                          tunnel and share the new host:port/web_url.
  TUNNEL_AUTH_FAILED    - SSH password rejected (password is fixed; check that
                          the SN is mapped to the right credential).
  TUNNEL_CMD_FAILED     - SSH up but device command failed (device-side issue).

Usage (inside backend container, /tools mounted):
    python /tools/dev_tunnel_check.py [host] [port]
"""
import asyncio
import sys

sys.path.insert(0, '/app')

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.integrations.credentials import get_credential_provider

SN = 'MACC1JZH3260M'
DEFAULT_HOST = '47.104.22.0'
DEFAULT_PORT = 65212


async def _check(host: str, port: int) -> int:
    provider = get_credential_provider()
    try:
        pwd = await provider.get_password(sn=SN, ip=host)
    except Exception as e:
        print(f'CREDENTIAL_ERROR: no password resolved for {SN}: {e}')
        return 4
    a = AsyncSSHDeviceAdapter(ip=host, port=port, username='root', password=pwd)
    try:
        await asyncio.wait_for(a.connect(), timeout=20)
        print('SSH connect: OK')
    except Exception as e:
        msg = str(e)
        if 'AUTH_FAILED' in msg or 'PermissionDenied' in msg:
            print(f'TUNNEL_AUTH_FAILED: {msg}')
            print('-> SSH password rejected (password is fixed; check SN->credential mapping).')
            return 3
        print(f'TUNNEL_UNREACHABLE: {msg}')
        print('-> Tunnel likely expired (stok/stamp/pass rotate ~3h). Re-open the EWEB')
        print('   tunnel and share the new host:port/web_url (e.g. via Feishu provision).')
        return 2
    try:
        await asyncio.wait_for(a.ensure_aim_session_ready(), timeout=20)
        r = await a.execute_cli('show version', timeout=10)
        if not (r.stdout or '').strip():
            print('TUNNEL_CMD_FAILED: AIM returned empty output (device-side issue).')
            return 4
        print('AIM command: OK')
    except Exception as e:
        print(f'TUNNEL_CMD_FAILED: {type(e).__name__}: {e}')
        print('-> SSH up but device command failed; likely device-side (AIM busy/loaded).')
        return 4
    finally:
        try:
            await a.disconnect()
        except Exception:
            pass
    print('TUNNEL_OK: SSH + AIM both work; reproduction can proceed.')
    return 0


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    print(f'checking tunnel {host}:{port} (SN={SN}) ...')
    return asyncio.run(_check(host, port))


if __name__ == '__main__':
    sys.exit(main())

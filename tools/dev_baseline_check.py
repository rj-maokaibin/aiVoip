"""One-off EC-02 device baseline check.
Reads credentials from /home/dev/secret.yaml (never printed) or the
DEV_PASSWORD/DEV_USER/DEV_HOST/DEV_PORT environment variables, connects with
legacy KEX, and runs read-only commands to confirm the device baseline.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import yaml
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter


def load_device(name: str):
    # Environment variables take precedence so the host secrets are not mounted
    # into the container.
    env = {
        'host': os.environ.get('DEV_HOST'),
        'sshport': os.environ.get('DEV_PORT'),
        'username': os.environ.get('DEV_USER'),
        'password': os.environ.get('DEV_PASSWORD'),
    }
    if all(env.values()):
        return env
    with open('/home/dev/secret.yaml') as f:
        d = yaml.safe_load(f)
    devs = d.get('device', d.get('devices', []))
    if isinstance(devs, dict):
        devs = list(devs.values())
    for dev in devs:
        if isinstance(dev, dict) and dev.get('name') == name:
            return dev
    raise SystemExit(f'device not found: {name}')


async def main():
    dev = load_device('voip-test-device')
    adapter = AsyncSSHDeviceAdapter(
        ip=dev['host'], port=int(dev['sshport']),
        username=dev['username'], password=dev['password'],
    )
    await adapter.connect()
    try:
        shell = await adapter.execute_shell(
            "echo '=== uname ==='; uname -a; "
            "echo '=== busybox ==='; busybox | head -1; "
            "echo '=== ip link voice ==='; ip -o link show br-lan_400 2>/dev/null || echo 'br-lan_400 not found'; "
            "echo '=== routes ==='; ip route | grep -i br-lan_400 || true"
        )
        print(shell.stdout)
        aim = await adapter.execute_cli('sys show bind-if')
        print('=== AIM bind-if ===')
        print(aim.stdout)
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

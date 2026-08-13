"""One-off EC-02 readonly action sweep.
Executes every registered L0 readonly action against the live DUT to confirm the
exact command is available and returns the expected evidence type. Read-only only.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.actions.registry import ActionRegistry
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout


ROOT = Path(__file__).resolve().parents[1]
SHELL_ACTIONS = [
    'CHECK_AIMD_PROCESS',
    'GET_VOIP_LOG',
    'GET_NETWORK_CONFIG',
    'GET_VOICE_VLAN_REF',
    'GET_ROUTE',
    'GET_DNS',
    'GET_NETWORK_VOIP_LOG',
    'GET_PCM_COUNTER',
    'GET_VOIP_SERVICE_INFO',
    'GET_VOICE_VLAN_CONFIG',
    'GET_INTERFACE_LINKS',
    'GET_BRIDGE_INFO',
]
AIM_ACTIONS = [
    'GET_AIM_BIND',
    'GET_AIM_SIP_CONFIG',
    'GET_SIP_REGISTER',
    'GET_DSP_RUNNING',
]


async def main():
    registry = ActionRegistry(ROOT / 'profiles')
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    await adapter.connect()
    try:
        results = []
        for aid in SHELL_ACTIONS:
            action = registry.action(aid)
            try:
                r = await adapter.execute_shell(action.command)
                head = (r.stdout or r.stderr).strip().splitlines()[:3]
                ok = r.exit_status == 0 and bool(r.stdout)
                results.append((aid, ok, ' | '.join(head)))
                print(f'[{"OK " if ok else "ERR"}] {aid}: {head[0] if head else "(no output)"}')
            except Exception as exc:
                results.append((aid, False, str(exc)))
                print(f'[ERR] {aid}: {exc}')
        # AIM commands via persistent PTY.
        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        for aid in AIM_ACTIONS:
            action = registry.action(aid)
            try:
                process.stdin.write(action.command + '\n')
                out = await read_until_prompt(stream, adapter.aim_prompt, 8)
                clean = out.rsplit(adapter.aim_prompt, 1)[0]
                head = clean.strip().splitlines()[:3]
                ok = bool(clean.strip()) and 'Error' not in clean
                results.append((aid, ok, ' | '.join(head)))
                print(f'[{"OK " if ok else "ERR"}] {aid}: {head[0] if head else "(no output)"}')
            except (PromptTimeout, Exception) as exc:
                results.append((aid, False, f'prompt/timeout {type(exc).__name__}'))
                print(f'[ERR] {aid}: prompt/timeout {type(exc).__name__}')
                await adapter._close_aim_session()
                process = await adapter._ensure_aim_session(10)
                stream = process.stdout
        print()
        print('=== SUMMARY ===')
        for aid, ok, _ in results:
            print(f'{"PASS" if ok else "FAIL"}  {aid}')
        failed = [aid for aid, ok, _ in results if not ok]
        print('failed:', failed if failed else '(none)')
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    asyncio.run(main())

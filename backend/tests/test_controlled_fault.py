import asyncio

import pytest

from app.collectors.device_adapter import CommandResult
from app.experiments.controlled_fault import (
    ControlledFaultError,
    SipGatewayEgressBlock,
    SipGatewayEgressBlockSpec,
)


class FakeAdapter:
    def __init__(self, *, preexisting=(), fail_delete=None, accepts_retries=True):
        self.rules = list(preexisting)
        self.fail_delete = fail_delete
        self.accepts_retries = accepts_retries
        self.calls = []

    def _save(self):
        lines = ["*filter", ":OUTPUT ACCEPT [0:0]"]
        lines.extend(f"-A OUTPUT {x}" for x in self.rules)
        lines.append("COMMIT")
        return "\n".join(lines) + "\n"

    async def execute_shell(self, command, timeout=None, **kwargs):
        if not self.accepts_retries and "retries" in kwargs:
            raise TypeError("unexpected retries")
        self.calls.append((command, kwargs.get("retries")))
        if command == "command -v iptables && command -v iptables-save":
            return CommandResult("/usr/sbin/iptables\n/usr/sbin/iptables-save\n", exit_status=0)
        if command == "iptables-save":
            return CommandResult(self._save(), exit_status=0)
        if command.startswith("iptables -C OUTPUT "):
            rule = command[len("iptables -C OUTPUT "):]
            return CommandResult("", exit_status=0 if rule in self.rules else 1)
        if command.startswith("iptables -I OUTPUT 1 "):
            rule = command[len("iptables -I OUTPUT 1 "):]
            self.rules.insert(0, rule)
            return CommandResult("", exit_status=0)
        if command.startswith("iptables -D OUTPUT "):
            rule = command[len("iptables -D OUTPUT "):]
            if self.fail_delete == rule:
                return CommandResult("", stderr="delete failed", exit_status=1)
            try:
                self.rules.remove(rule)
            except ValueError:
                return CommandResult("", exit_status=1)
            return CommandResult("", exit_status=0)
        raise AssertionError(f"unexpected command: {command}")


def _spec():
    return SipGatewayEgressBlockSpec(
        gateway_ip="192.168.3.200",
        voice_interface="br-lan_400",
        port=5060,
    )


def test_spec_rejects_shell_injection_and_invalid_values():
    with pytest.raises(ControlledFaultError, match="INVALID_GATEWAY"):
        SipGatewayEgressBlockSpec("192.168.3.200;reboot", "br-lan_400")
    with pytest.raises(ControlledFaultError, match="INVALID_INTERFACE"):
        SipGatewayEgressBlockSpec("192.168.3.200", "br-lan_400;reboot")
    with pytest.raises(ControlledFaultError, match="INVALID_PORT"):
        SipGatewayEgressBlockSpec("192.168.3.200", "br-lan_400", 0)
    with pytest.raises(ControlledFaultError, match="INVALID_TRANSPORT"):
        SipGatewayEgressBlockSpec("192.168.3.200", "br-lan_400", transports=("icmp",))


def test_apply_and_restore_are_exact_nonretried_and_hash_restored():
    adapter = FakeAdapter()
    fault = SipGatewayEgressBlock(adapter, _spec())

    async def scenario():
        pre = await fault.preflight()
        applied = await fault.apply()
        assert fault.cleanup_required is True
        restored = await fault.restore()
        return pre, applied, restored

    pre, applied, restored = asyncio.run(scenario())
    assert pre["preexisting_rule"] is False
    assert applied["state"] == "APPLIED_VERIFIED"
    assert applied["transports"] == ["udp", "tcp"]
    assert restored["state"] == "RESTORED_VERIFIED"
    assert restored["restored_ruleset_sha256"] == pre["baseline_ruleset_sha256"]
    assert adapter.rules == []
    assert fault.cleanup_required is False
    assert all(retries == 0 for _, retries in adapter.calls)
    assert any(
        cmd == "iptables -I OUTPUT 1 -o br-lan_400 -p udp -d 192.168.3.200 --dport 5060 -j DROP"
        for cmd, _ in adapter.calls
    )
    assert any(
        cmd == "iptables -I OUTPUT 1 -o br-lan_400 -p tcp -d 192.168.3.200 --dport 5060 -j DROP"
        for cmd, _ in adapter.calls
    )


def test_preexisting_identical_rule_fails_closed_without_mutation():
    udp_rule = "-o br-lan_400 -p udp -d 192.168.3.200 --dport 5060 -j DROP"
    adapter = FakeAdapter(preexisting=(udp_rule,))
    fault = SipGatewayEgressBlock(adapter, _spec())

    with pytest.raises(ControlledFaultError, match="PREEXISTING_RULE:udp"):
        asyncio.run(fault.preflight())

    assert adapter.rules == [udp_rule]
    assert not any(cmd.startswith("iptables -I ") for cmd, _ in adapter.calls)
    assert not any(cmd.startswith("iptables -D ") for cmd, _ in adapter.calls)


def test_cleanup_failure_preserves_ownership_for_retry():
    tcp_rule = "-o br-lan_400 -p tcp -d 192.168.3.200 --dport 5060 -j DROP"
    adapter = FakeAdapter(fail_delete=tcp_rule)
    fault = SipGatewayEgressBlock(adapter, _spec())

    async def scenario():
        await fault.apply()
        with pytest.raises(ControlledFaultError, match="CLEANUP_UNVERIFIED"):
            await fault.restore()
        assert fault.cleanup_required is True
        adapter.fail_delete = None
        restored = await fault.restore()
        assert restored["state"] == "RESTORED_VERIFIED"

    asyncio.run(scenario())
    assert adapter.rules == []
    assert fault.cleanup_required is False


def test_adapter_must_support_nonretry_mutation_contract():
    adapter = FakeAdapter(accepts_retries=False)
    fault = SipGatewayEgressBlock(adapter, _spec())
    with pytest.raises(ControlledFaultError, match="ADAPTER_NO_NONRETRY_MUTATION"):
        asyncio.run(fault.preflight())

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.collectors.device_adapter import CommandResult
from app.infrastructure.action_route import (
    ActionBackend,
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
    RunIntent,
)
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.config_framework.schema import (
    ConfigFrameworkDomainError,
    ConfigFrameworkParseError,
    mask_secrets,
    parse_config_result,
)
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.mutation.contract import MutationOperationPolicy, MutationStatus, ReadOperationPolicy
from app.infrastructure.transport.ssh import SharedSshTransport


@dataclass(frozen=True)
class FakeToken:
    device_id: str = "dut-1"
    capture_session_id: str = "run-1"
    owner_worker_id: str = "worker-1"
    lease_epoch: int = 7
    expires_at: datetime = datetime(2030, 1, 1, tzinfo=timezone.utc)


class FakeSshAdapter:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def connect(self):
        self.calls.append(("connect",))

    async def disconnect(self):
        self.calls.append(("disconnect",))

    async def execute_shell(self, command, timeout=None, retries=2):
        self.calls.append(("shell", command, timeout, retries))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def execute_cli(self, command, timeout=None):
        self.calls.append(("cli", command, timeout))
        return CommandResult(stdout="ok")

    async def sftp_get(self, remote_path, local_path, timeout=None):
        self.calls.append(("sftp_get", remote_path, local_path, timeout))

    async def scp_get(self, remote_path, local_path, timeout=None):
        self.calls.append(("scp_get", remote_path, local_path, timeout))


class FakeAuthority:
    def __init__(self):
        self.validated = []

    def acquire(self, **kwargs):
        return FakeToken()

    def renew(self, token):
        return token

    def validate(self, token):
        self.validated.append(token)
        return token

    def release(self, token):
        return None


class FakeLeaseManager:
    def __init__(self):
        self.calls = []
        self.token = FakeToken()

    def acquire(self, **kwargs):
        self.calls.append(("acquire", kwargs))
        return self.token

    def renew(self, token):
        self.calls.append(("renew", token))
        return token

    def validate(self, token):
        self.calls.append(("validate", token))
        return token

    def release(self, token):
        self.calls.append(("release", token))


def run(coro):
    return asyncio.run(coro)


def test_route_contract_has_only_frozen_dimensions_and_no_diagnosis_purpose():
    route = ActionRoute(
        entry=ActionEntry.NONE,
        transport=ActionTransport.SSH,
        backend=ActionBackend.CONFIG_FRAMEWORK,
        purpose=ActionPurpose.OBSERVATION,
        target="voip",
    )
    assert route.as_dict() == {
        "entry": "none",
        "transport": "ssh",
        "backend": "config_framework",
        "purpose": "observation",
        "target": "voip",
    }
    assert {item.value for item in RunIntent} == {"diagnose", "verify", "reproduce"}
    assert "diagnosis" not in {item.value for item in ActionPurpose}


def test_shared_ssh_facade_delegates_exactly_and_does_not_add_retry():
    adapter = FakeSshAdapter()
    adapter.responses.append(CommandResult(stdout="ok"))
    transport = SharedSshTransport(adapter)
    result = run(transport.execute("echo ok", timeout=4.5, retries=0))
    assert result.stdout == "ok"
    assert adapter.calls == [("shell", "echo ok", 4.5, 0)]


def test_device_authority_adapter_maps_run_to_existing_capture_session_and_preserves_token():
    manager = FakeLeaseManager()
    adapter = CaptureLeaseCompatibilityAdapter(manager)  # type: ignore[arg-type]
    token = adapter.acquire(device_id="dut-1", run_id="run-1", owner_worker_id="worker-1")
    assert token is manager.token
    assert manager.calls[0] == (
        "acquire",
        {"device_id": "dut-1", "capture_session_id": "run-1", "owner_worker_id": "worker-1"},
    )
    assert adapter.renew(token) is token
    assert adapter.validate(token) is token
    adapter.release(token)
    assert [call[0] for call in manager.calls] == ["acquire", "renew", "validate", "release"]


def test_device_authority_adapter_propagates_lease_busy_without_parallel_lock_state():
    class BusyManager(FakeLeaseManager):
        def acquire(self, **kwargs):
            raise RuntimeError("LEASE_BUSY")

    adapter = CaptureLeaseCompatibilityAdapter(BusyManager())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="LEASE_BUSY"):
        adapter.acquire(device_id="dut-1", run_id="run-2", owner_worker_id="worker-2")


def test_read_policy_retries_bounded_transient_failure():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("read timeout")
        return "ok"

    value = run(ReadOperationPolicy(max_attempts=3).execute(operation))
    assert value == "ok"
    assert calls == 3


def test_mutation_timeout_observes_state_and_never_blind_retries():
    authority = FakeAuthority()
    token = FakeToken()
    mutate_calls = 0
    observe_calls = 0

    async def mutate():
        nonlocal mutate_calls
        mutate_calls += 1
        raise TimeoutError("unknown result")

    async def observe():
        nonlocal observe_calls
        observe_calls += 1
        return {"number": "2001"}

    result = run(
        MutationOperationPolicy(authority).execute(
            token=token,
            mutate=mutate,
            observe=observe,
            is_applied=lambda value: value.get("number") == "2002",
        )
    )
    assert result.status is MutationStatus.UNKNOWN
    assert result.observed_after_unknown is True
    assert mutate_calls == 1
    assert observe_calls == 1
    assert authority.validated == [token]


def test_config_result_parser_success_and_domain_failure():
    success = parse_config_result('{"rcode":"00000000","rmsg":"success","data":{"x":1}}')
    assert success.success is True
    failed = parse_config_result('{"rcode":"02870001","rmsg":"invalid parameter"}')
    assert failed.success is False
    with pytest.raises(ConfigFrameworkParseError):
        parse_config_result("not-json")


def test_config_set_uses_safe_shell_quoting_and_masks_secrets():
    adapter = FakeSshAdapter()
    executor = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules={"voipUserInfo"},
        authority=FakeAuthority(),
    )
    payload = {
        "data": [{"number": "2001'; touch /tmp/pwned; echo '", "passwd": "s3cr'et"}],
        "token": "top-secret",
    }
    command = executor.build_set_command("voipUserInfo", payload)
    argv = shlex.split(command)
    assert argv[:4] == ["dev_config", "set", "-m", "voipUserInfo"]
    assert json.loads(argv[4]) == payload
    masked = executor.build_masked_set_command("voipUserInfo", payload)
    assert "s3cr'et" not in masked
    assert "top-secret" not in masked
    assert json.loads(shlex.split(masked)[4])["data"][0]["passwd"] == "***"


def test_config_get_retries_read_but_set_is_single_attempt_on_unknown():
    adapter = FakeSshAdapter()
    authority = FakeAuthority()
    executor = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules={"voipUserInfo"},
        authority=authority,
        read_policy=ReadOperationPolicy(max_attempts=2),
    )
    # set attempt times out, then observation GET succeeds and shows old value.
    adapter.responses.extend(
        [
            TimeoutError("SSH_COMMAND_TIMEOUT"),
            CommandResult(stdout='{"rcode":"00000000","rmsg":"success","data":{"number":"2001"}}'),
        ]
    )
    result = run(
        executor.set(
            "voipUserInfo",
            {"number": "2002"},
            authority_token=FakeToken(),
        )
    )
    assert result.status is MutationStatus.UNKNOWN
    shell_calls = [call for call in adapter.calls if call[0] == "shell"]
    assert len(shell_calls) == 2
    assert shell_calls[0][3] == 0  # mutation never blind retries
    assert shell_calls[1][1] == "dev_config get -m voipUserInfo"


def test_config_set_timeout_readback_can_prove_already_applied():
    adapter = FakeSshAdapter()
    executor = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules={"voipUserInfo"},
        authority=FakeAuthority(),
        read_policy=ReadOperationPolicy(max_attempts=1),
    )
    adapter.responses.extend(
        [
            TimeoutError("SSH_COMMAND_TIMEOUT"),
            CommandResult(stdout='{"rcode":"00000000","rmsg":"success","data":{"number":"2002"}}'),
        ]
    )
    result = run(executor.set("voipUserInfo", {"number": "2002"}, authority_token=FakeToken()))
    assert result.status is MutationStatus.APPLIED
    assert result.observed_after_unknown is True
    assert result.success is True


def test_config_domain_failure_is_not_treated_as_unknown_and_not_retried():
    adapter = FakeSshAdapter()
    executor = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules={"voipUserInfo"},
        authority=FakeAuthority(),
    )
    adapter.responses.append(CommandResult(stdout='{"rcode":"02870001","rmsg":"invalid parameter"}'))
    with pytest.raises(ConfigFrameworkDomainError):
        run(executor.set("voipUserInfo", {"number": "x"}, authority_token=FakeToken()))
    assert len([call for call in adapter.calls if call[0] == "shell"]) == 1


def test_secret_masking_is_recursive_and_structure_preserving():
    source = {"user": "2001", "passwd": "pw", "nested": {"Authorization": "Bearer abc", "x": 1}}
    masked = mask_secrets(source)
    assert masked == {"user": "2001", "passwd": "***", "nested": {"Authorization": "***", "x": 1}}
    assert "pw" not in json.dumps(masked)
    assert "Bearer abc" not in json.dumps(masked)


def test_arbitrary_config_module_is_rejected_before_transport():
    adapter = FakeSshAdapter()
    executor = ConfigFrameworkExecutor(SharedSshTransport(adapter), allowed_modules={"voipUserInfo"})
    with pytest.raises(ValueError, match="CONFIG_MODULE_NOT_ALLOWED"):
        executor.build_get_command("$(reboot)")
    assert adapter.calls == []

from __future__ import annotations

import asyncio

import pytest

from app.collectors.device_adapter import CommandResult
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.config_framework.schema import ConfigFrameworkParseError, parse_config_result


class _ReadTransport:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[tuple[str, int]] = []

    async def execute(self, command: str, *, timeout=None, retries=0):
        self.calls.append((command, retries))
        return CommandResult(stdout=self.stdout, exit_status=0)


def test_data_only_get_response_is_normalized_to_success() -> None:
    result = parse_config_result(
        '{"data":[{"hdl":0,"disName":"7102","number":"7102","passwd":"secret"}]}',
        allow_data_only=True,
    )
    assert result.success is True
    assert result.rcode == "00000000"
    assert result.rmsg == "success"
    assert result.data[0]["number"] == "7102"


def test_data_only_response_is_still_rejected_for_mutation_acknowledgement() -> None:
    with pytest.raises(ConfigFrameworkParseError, match="CONFIG_FRAMEWORK_RESPONSE_INVALID"):
        parse_config_result('{"data":[{"hdl":0}]}')


def test_get_executor_uses_read_only_data_only_mode_without_transport_retry() -> None:
    transport = _ReadTransport('{"data":[{"hdl":0,"disName":"7102"}]}')
    executor = ConfigFrameworkExecutor(transport, allowed_modules=("voipUserInfo",))
    result = asyncio.run(executor.get("voipUserInfo", timeout=20.0))
    assert result.success is True
    assert result.data == [{"hdl": 0, "disName": "7102"}]
    assert transport.calls == [("dev_config get -m voipUserInfo", 0)]


def test_wrapped_framework_response_remains_supported_for_reads_and_mutations() -> None:
    result = parse_config_result('{"rcode":"00000000","rmsg":"success","data":[]}')
    assert result.success is True
    assert result.data == []


def test_data_only_mode_does_not_accept_arbitrary_json_object() -> None:
    with pytest.raises(ConfigFrameworkParseError, match="CONFIG_FRAMEWORK_RESPONSE_INVALID"):
        parse_config_result('{"unexpected":true}', allow_data_only=True)

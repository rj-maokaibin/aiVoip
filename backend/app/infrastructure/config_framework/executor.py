from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from typing import Any, Callable, Iterable, TypeVar

from app.collectors.device_adapter import CommandResult
from app.infrastructure.config_framework.schema import (
    ConfigFrameworkDomainError,
    ConfigFrameworkError,
    ConfigMutationResult,
    ConfigResult,
    mask_secrets,
    parse_config_result,
)
from app.infrastructure.device_authority.base import DeviceAuthority
from app.infrastructure.mutation.contract import (
    MutationOperationPolicy,
    MutationStatus,
    ReadOperationPolicy,
)
from app.infrastructure.transport.ssh import SharedSshTransport


TokenT = TypeVar("TokenT")


class ConfigFrameworkExecutor:
    """Thin dev_config backend adapter.

    Module allowlisting belongs to Semantic Action configuration and is injected
    here.  This class does not encode VOIP test semantics or product-entry logic.
    """

    def __init__(
        self,
        transport: SharedSshTransport,
        *,
        allowed_modules: Iterable[str],
        authority: DeviceAuthority[TokenT] | None = None,
        read_policy: ReadOperationPolicy | None = None,
    ):
        modules = frozenset(str(module) for module in allowed_modules)
        if not modules:
            raise ValueError("CONFIG_MODULE_ALLOWLIST_EMPTY")
        self._transport = transport
        self._allowed_modules = modules
        self._authority = authority
        self._read_policy = read_policy or ReadOperationPolicy(max_attempts=3)

    @property
    def allowed_modules(self) -> frozenset[str]:
        return self._allowed_modules

    def _require_module(self, module: str) -> str:
        if module not in self._allowed_modules:
            raise ValueError(f"CONFIG_MODULE_NOT_ALLOWED:{module}")
        return module

    def build_get_command(self, module: str) -> str:
        module = self._require_module(module)
        return f"dev_config get -m {shlex.quote(module)}"

    def build_set_command(self, module: str, payload: Mapping[str, Any]) -> str:
        module = self._require_module(module)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return f"dev_config set -m {shlex.quote(module)} {shlex.quote(encoded)}"

    def build_masked_set_command(self, module: str, payload: Mapping[str, Any]) -> str:
        return self.build_set_command(module, mask_secrets(payload))

    @staticmethod
    def _transport_unknown(exc: Exception) -> bool:
        text = str(exc).upper()
        return isinstance(exc, TimeoutError) or "TIMEOUT" in text or "CONNECTION" in text

    @staticmethod
    def _subset(expected: Any, actual: Any) -> bool:
        if isinstance(expected, Mapping):
            if not isinstance(actual, Mapping):
                return False
            return all(key in actual and ConfigFrameworkExecutor._subset(value, actual[key]) for key, value in expected.items())
        if isinstance(expected, list):
            if not isinstance(actual, list) or len(expected) != len(actual):
                return False
            return all(ConfigFrameworkExecutor._subset(left, right) for left, right in zip(expected, actual))
        return expected == actual

    @classmethod
    def payload_matches_readback(cls, payload: Mapping[str, Any], result: ConfigResult) -> bool:
        if not result.success:
            return False
        if isinstance(result.data, Mapping) and cls._subset(payload, result.data):
            return True
        return cls._subset(payload, result.raw)

    @staticmethod
    def _parse_command_result(
        result: CommandResult,
        *,
        allow_data_only: bool = False,
    ) -> ConfigResult:
        if result.exit_status != 0:
            raise ConfigFrameworkError(f"CONFIG_FRAMEWORK_COMMAND_EXIT:{result.exit_status}")
        return parse_config_result(result.stdout, allow_data_only=allow_data_only)

    async def get(self, module: str, *, timeout: float | None = None) -> ConfigResult:
        command = self.build_get_command(module)

        async def _read_once() -> ConfigResult:
            result = await self._transport.execute(command, timeout=timeout, retries=0)
            return self._parse_command_result(result, allow_data_only=True)

        parsed = await self._read_policy.execute(_read_once, retry_if=self._transport_unknown)
        return parsed

    async def set(
        self,
        module: str,
        payload: Mapping[str, Any],
        *,
        authority_token: TokenT,
        timeout: float | None = None,
        readback_matcher: Callable[[ConfigResult], bool] | None = None,
    ) -> ConfigMutationResult:
        if self._authority is None:
            raise RuntimeError("CONFIG_MUTATION_AUTHORITY_REQUIRED")
        command = self.build_set_command(module, payload)
        policy = MutationOperationPolicy(self._authority)
        matcher = readback_matcher or (lambda result: self.payload_matches_readback(payload, result))

        async def _mutate_once() -> ConfigResult:
            # Mutation is one SSH attempt. Never inherit AsyncSSHDeviceAdapter's
            # read/capture default retries.
            command_result = await self._transport.execute(command, timeout=timeout, retries=0)
            parsed = self._parse_command_result(command_result)
            if not parsed.success:
                raise ConfigFrameworkDomainError(parsed)
            return parsed

        execution = await policy.execute(
            token=authority_token,
            mutate=_mutate_once,
            observe=lambda: self.get(module, timeout=timeout),
            is_applied=matcher,
            is_unknown_error=self._transport_unknown,
        )
        if execution.status is MutationStatus.APPLIED:
            return ConfigMutationResult(
                status=MutationStatus.APPLIED,
                response=execution.value,
                readback=execution.observation,
                observed_after_unknown=execution.observed_after_unknown,
            )
        return ConfigMutationResult(
            status=MutationStatus.UNKNOWN,
            readback=execution.observation,
            observed_after_unknown=True,
        )

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any


class ControlledFaultError(RuntimeError):
    """Fail-closed error for an allowlisted laboratory DUT fault."""


@dataclass(frozen=True)
class SipGatewayEgressBlockSpec:
    gateway_ip: str
    voice_interface: str
    port: int = 5060
    transports: tuple[str, ...] = ("udp", "tcp")

    def __post_init__(self) -> None:
        try:
            ipaddress.IPv4Address(self.gateway_ip)
        except Exception as exc:
            raise ControlledFaultError("CONTROLLED_FAULT_INVALID_GATEWAY") from exc
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", self.voice_interface or ""):
            raise ControlledFaultError("CONTROLLED_FAULT_INVALID_INTERFACE")
        if not (1 <= int(self.port) <= 65535):
            raise ControlledFaultError("CONTROLLED_FAULT_INVALID_PORT")
        if not self.transports or any(x not in {"udp", "tcp"} for x in self.transports):
            raise ControlledFaultError("CONTROLLED_FAULT_INVALID_TRANSPORT")
        if len(set(self.transports)) != len(self.transports):
            raise ControlledFaultError("CONTROLLED_FAULT_DUPLICATE_TRANSPORT")


class SipGatewayEgressBlock:
    """One finite, reversible DUT-only laboratory fault.

    Safety contract:
    - destination is one validated IPv4 gateway and one SIP port;
    - egress interface is validated and supplied by resolved voice runtime context;
    - only UDP/TCP can be blocked;
    - a pre-existing identical rule is a hard conflict (never delete operator state);
    - mutation commands are non-retried;
    - every apply is verified;
    - cleanup deletes only exact rules and must restore the whole iptables-save hash;
    - cleanup failure preserves ownership so a reconciler can retry;
    - any ambiguity fails closed.

    The adapter is expected to provide execute_shell(command, timeout=..., retries=0).
    This intentionally does not expose arbitrary shell input to callers.
    """

    action_id = "SIP_GATEWAY_EGRESS_BLOCK_V1"

    def __init__(self, adapter: Any, spec: SipGatewayEgressBlockSpec, *, timeout: float = 8.0):
        self.adapter = adapter
        self.spec = spec
        self.timeout = timeout
        self._baseline_hash: str | None = None
        self._applied: list[str] = []

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def cleanup_required(self) -> bool:
        return bool(self._applied)

    def _rule_args(self, transport: str) -> str:
        s = self.spec
        return (
            f"-o {s.voice_interface} -p {transport} -d {s.gateway_ip} "
            f"--dport {int(s.port)} -j DROP"
        )

    async def _exec(self, command: str):
        # Controlled mutations MUST NOT inherit execute_shell's retry behavior: an
        # SSH timeout after a successful mutation is ambiguous and must fail closed.
        try:
            return await self.adapter.execute_shell(command, timeout=self.timeout, retries=0)
        except TypeError as exc:
            raise ControlledFaultError("CONTROLLED_FAULT_ADAPTER_NO_NONRETRY_MUTATION") from exc

    async def _iptables_save(self) -> str:
        result = await self._exec("iptables-save")
        if result.exit_status != 0:
            raise ControlledFaultError("CONTROLLED_FAULT_IPTABLES_SAVE_FAILED")
        return result.stdout or ""

    async def preflight(self) -> dict[str, Any]:
        path = await self._exec("command -v iptables && command -v iptables-save")
        if path.exit_status != 0 or "iptables" not in (path.stdout or ""):
            raise ControlledFaultError("CONTROLLED_FAULT_IPTABLES_UNAVAILABLE")

        baseline = await self._iptables_save()
        self._baseline_hash = self._hash_text(baseline)

        conflicts: list[str] = []
        for transport in self.spec.transports:
            check = await self._exec(f"iptables -C OUTPUT {self._rule_args(transport)}")
            if check.exit_status == 0:
                conflicts.append(transport)
        if conflicts:
            raise ControlledFaultError(
                "CONTROLLED_FAULT_PREEXISTING_RULE:" + ",".join(sorted(conflicts))
            )

        return {
            "action_id": self.action_id,
            "gateway_ip": self.spec.gateway_ip,
            "voice_interface": self.spec.voice_interface,
            "port": self.spec.port,
            "transports": list(self.spec.transports),
            "baseline_ruleset_sha256": self._baseline_hash,
            "preexisting_rule": False,
        }

    async def apply(self) -> dict[str, Any]:
        if self._baseline_hash is None:
            await self.preflight()
        if self._applied:
            raise ControlledFaultError("CONTROLLED_FAULT_ALREADY_APPLIED")

        try:
            for transport in self.spec.transports:
                args = self._rule_args(transport)
                result = await self._exec(f"iptables -I OUTPUT 1 {args}")
                if result.exit_status != 0:
                    raise ControlledFaultError(
                        f"CONTROLLED_FAULT_APPLY_FAILED:{transport}:{result.exit_status}"
                    )
                self._applied.append(transport)
                verify = await self._exec(f"iptables -C OUTPUT {args}")
                if verify.exit_status != 0:
                    raise ControlledFaultError(
                        f"CONTROLLED_FAULT_APPLY_VERIFY_FAILED:{transport}"
                    )
        except Exception:
            # Best-effort immediate reversal. If cleanup itself is uncertain, keep
            # ownership in self._applied so the caller/reconciler can retry.
            try:
                await self.restore()
            except Exception as cleanup_exc:
                raise ControlledFaultError(
                    f"CONTROLLED_FAULT_APPLY_AND_CLEANUP_FAILED:{type(cleanup_exc).__name__}:{cleanup_exc}"
                ) from cleanup_exc
            raise

        active = await self._iptables_save()
        return {
            "action_id": self.action_id,
            "state": "APPLIED_VERIFIED",
            "gateway_ip": self.spec.gateway_ip,
            "voice_interface": self.spec.voice_interface,
            "port": self.spec.port,
            "transports": list(self._applied),
            "baseline_ruleset_sha256": self._baseline_hash,
            "active_ruleset_sha256": self._hash_text(active),
        }

    async def restore(self) -> dict[str, Any]:
        errors: list[str] = []
        remaining: list[str] = []
        for transport in reversed(self._applied):
            args = self._rule_args(transport)
            result = await self._exec(f"iptables -D OUTPUT {args}")
            if result.exit_status != 0:
                errors.append(f"delete:{transport}:{result.exit_status}")
                remaining.append(transport)
                continue
            verify = await self._exec(f"iptables -C OUTPUT {args}")
            if verify.exit_status == 0:
                errors.append(f"still_present:{transport}")
                remaining.append(transport)

        self._applied = list(reversed(remaining))
        current = await self._iptables_save()
        current_hash = self._hash_text(current)
        if self._baseline_hash is not None and current_hash != self._baseline_hash:
            errors.append("ruleset_hash_mismatch")
        if errors:
            raise ControlledFaultError("CONTROLLED_FAULT_CLEANUP_UNVERIFIED:" + ";".join(errors))

        self._applied.clear()
        return {
            "action_id": self.action_id,
            "state": "RESTORED_VERIFIED",
            "gateway_ip": self.spec.gateway_ip,
            "voice_interface": self.spec.voice_interface,
            "port": self.spec.port,
            "transports": list(self.spec.transports),
            "restored_ruleset_sha256": current_hash,
            "baseline_ruleset_sha256": self._baseline_hash,
        }

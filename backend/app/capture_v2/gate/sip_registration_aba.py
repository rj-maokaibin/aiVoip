from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.analyzers.packet.engine import PacketIntelligenceEngine
from app.capture_v2.db_models import CaptureSession
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.factory import build_capture_v2_ab
from app.capture_v2.gate.models import GateCaseResult, GateCheck, GateDeviceSpec, GateRunPaths, GateVerdict
from app.capture_v2.profiles.resolver import EffectiveProfileResolver
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.capture_v2.voice_context import VoiceContextResolverV2


PROFILE_ID = "SIP_REGISTRATION_EGRESS_BLOCK_ABA"
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _one_line(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        raise CaptureV2Error("SIP_ABA_INVARIANT_VALUE_INVALID", details={"field": field})
    return text


def _parse_endpoint(value: str) -> tuple[str, int]:
    text = str(value or "").strip()
    host, sep, port_text = text.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise CaptureV2Error("SIP_ABA_REGISTRAR_ENDPOINT_INVALID", details={"endpoint": text})
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise CaptureV2Error("SIP_ABA_REGISTRAR_IP_INVALID", details={"endpoint": text}) from exc
    if ip.version != 4:
        raise CaptureV2Error("SIP_ABA_IPV6_REGISTRAR_NOT_SUPPORTED", details={"registrar": str(ip)})
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise CaptureV2Error("SIP_ABA_REGISTRAR_PORT_INVALID", details={"port": port})
    return str(ip), port


def _registration_request_endpoint(registration: dict[str, Any]) -> tuple[str, int]:
    endpoints: set[tuple[str, int]] = set()
    for row in registration.get("ladder") or []:
        if str(row.get("method") or "").upper() == "REGISTER":
            endpoints.add(_parse_endpoint(str(row.get("dst") or "")))
    if len(endpoints) != 1:
        raise CaptureV2Error(
            "SIP_ABA_REGISTRAR_ENDPOINT_AMBIGUOUS",
            details={"endpoints": [f"{ip}:{port}" for ip, port in sorted(endpoints)]},
        )
    return next(iter(endpoints))


def select_healthy_registrar(analysis: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    healthy: list[tuple[str, int, dict[str, Any]]] = []
    for registration in analysis.get("registrations") or []:
        if registration.get("status") != "SUCCESS":
            continue
        ip, port = _registration_request_endpoint(registration)
        healthy.append((ip, port, registration))
    unique = {(ip, port) for ip, port, _ in healthy}
    if len(unique) != 1:
        raise CaptureV2Error(
            "SIP_ABA_BASELINE_REGISTRAR_NOT_UNIQUE",
            details={
                "healthy_registration_count": len(healthy),
                "registrars": [f"{ip}:{port}" for ip, port in sorted(unique)],
            },
        )
    ip, port = next(iter(unique))
    chosen = next(reg for candidate_ip, candidate_port, reg in healthy if (candidate_ip, candidate_port) == (ip, port))
    return ip, port, chosen


def registrar_success_observed(analysis: dict[str, Any], *, registrar_ip: str, registrar_port: int) -> bool:
    for registration in analysis.get("registrations") or []:
        try:
            endpoint = _registration_request_endpoint(registration)
        except CaptureV2Error:
            continue
        if endpoint == (registrar_ip, registrar_port) and registration.get("status") == "SUCCESS":
            return True
    return False


@dataclass(frozen=True)
class FirewallRule:
    registrar_ip: str
    registrar_port: int
    transport: str
    comment: str

    def __post_init__(self) -> None:
        ip = ipaddress.ip_address(self.registrar_ip)
        if ip.version != 4:
            raise CaptureV2Error("SIP_ABA_IPV6_REGISTRAR_NOT_SUPPORTED")
        if not 1 <= int(self.registrar_port) <= 65535:
            raise CaptureV2Error("SIP_ABA_REGISTRAR_PORT_INVALID")
        if self.transport not in {"udp", "tcp"}:
            raise CaptureV2Error("SIP_ABA_TRANSPORT_INVALID", details={"transport": self.transport})
        if not _SAFE_TOKEN.fullmatch(self.comment):
            raise CaptureV2Error("SIP_ABA_RULE_COMMENT_INVALID")

    @property
    def args(self) -> str:
        return (
            f"-p {self.transport} -d {shlex.quote(self.registrar_ip)} "
            f"--dport {int(self.registrar_port)} -m comment --comment {shlex.quote(self.comment)} -j DROP"
        )

    def insert_command(self) -> str:
        return f"iptables -w 5 -I OUTPUT 1 {self.args}"

    def check_command(self) -> str:
        return f"iptables -w 5 -C OUTPUT {self.args}"

    def delete_command(self) -> str:
        return f"iptables -w 5 -D OUTPUT {self.args}"

    def counter_command(self) -> str:
        marker = shlex.quote(self.comment)
        return (
            "iptables -w 5 -nvx -L OUTPUT | "
            f"awk -v marker={marker} '$0 ~ marker {{print $1; found=1}} END {{if (!found) exit 44}}'"
        )


@dataclass(frozen=True)
class PhaseEvidence:
    phase: str
    pcap: str
    pcap_sha256: str
    analysis: dict[str, Any]
    started_at: str
    ended_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "pcap": self.pcap,
            "pcap_sha256": self.pcap_sha256,
            "analysis": self.analysis,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class SipRegistrationABAGate:
    """Real-DUT SIP registration A-B-A causal gate.

    A1 proves REGISTER -> 2xx and resolves exactly one registrar endpoint. B adds
    exactly one non-persistent DUT-local OUTPUT DROP rule for that endpoint and
    requires the rule packet counter to increase while REGISTER success disappears.
    The exact rule is then removed and A2 must prove REGISTER -> 2xx recovery.

    The gate never mutates a PBX, default route, persistent firewall configuration,
    or any destination other than the A1-observed registrar endpoint.
    """

    def __init__(
        self,
        *,
        session_factory,
        adapter,
        profile_root: Path,
        requested_profile_id: str,
        output_root: Path,
    ):
        self.session_factory = session_factory
        self.adapter = adapter
        self.profile_root = Path(profile_root)
        self.requested_profile_id = requested_profile_id
        self.output_root = Path(output_root)
        self.reader = ReadOnlyDeviceTransport(adapter)

    async def _exec_status(self, command: str) -> int:
        result = await self.adapter.execute_shell(command, retries=0)
        return int(result.exit_status or 0)

    async def _read_invariants(
        self,
        *,
        device: GateDeviceSpec,
        serial_command: str,
        software_version_command: str,
    ) -> dict[str, str]:
        if not serial_command.strip() or not software_version_command.strip():
            raise CaptureV2Error("SIP_ABA_INVARIANT_COMMAND_REQUIRED")
        serial = _one_line(await self.reader.run(serial_command), field="device.serial")
        version = _one_line(await self.reader.run(software_version_command), field="software.version")
        voice = await VoiceContextResolverV2(self.reader).resolve()
        return {
            "device.serial": serial,
            "device.id": str(device.device_id),
            "software.version": version,
            "voice.voice_vlan_id": _one_line(voice.voice_vlan_id, field="voice.voice_vlan_id"),
            "voice.gateway_ip": _one_line(voice.gateway_ip, field="voice.gateway_ip"),
            "voice.interface": _one_line(voice.interface, field="voice.interface"),
        }

    def _new_capture_session(self, *, reproduction_session_id: str, device: GateDeviceSpec):
        with self.session_factory() as db:
            existing = db.scalar(
                select(CaptureSession)
                .where(CaptureSession.reproduction_session_id == reproduction_session_id)
                .limit(1)
            )
        if existing is not None:
            raise CaptureV2Error(
                "SIP_ABA_REPRODUCTION_SESSION_ALREADY_OWNED",
                details={"reproduction_session_id": reproduction_session_id, "capture_session_id": str(existing.id)},
            )
        effective = EffectiveProfileResolver(self.profile_root).resolve(
            device=device.as_profile_device(),
            requested_profile_id=self.requested_profile_id,
        )
        supervisor = build_capture_v2_ab(
            adapter=self.adapter,
            effective_profile=effective,
            lease_ttl_seconds=900.0,
        )
        capture_session_id = supervisor.create_session(
            reproduction_session_id=reproduction_session_id,
            device_id=device.device_id,
            effective_profile=effective,
        )
        return supervisor, capture_session_id

    async def _capture_phase(
        self,
        *,
        phase: str,
        token,
        mutator,
        interface: str,
        phase_seconds: float,
        trigger_command: str,
        paths: GateRunPaths,
    ) -> PhaseEvidence:
        safe_phase = phase.lower()
        remote_dir = f"/tmp/aivoip_sip_aba/{token.capture_session_id}"
        remote_pcap = f"{remote_dir}/{safe_phase}.pcap"
        remote_pid = f"{remote_dir}/{safe_phase}.pid"
        remote_log = f"{remote_dir}/{safe_phase}.tcpdump.log"
        q_dir = shlex.quote(remote_dir)
        q_pcap = shlex.quote(remote_pcap)
        q_pid = shlex.quote(remote_pid)
        q_log = shlex.quote(remote_log)
        q_interface = shlex.quote(interface)

        existing = await self.reader.list_tcpdump_processes()
        if existing:
            raise CaptureV2Error(
                "SIP_ABA_EXISTING_TCPDUMP_PRESENT",
                details={"pids": [row.pid for row in existing]},
            )

        start_body = (
            f"mkdir -p {q_dir}; rm -f {q_pcap} {q_pid} {q_log}; "
            f"tcpdump -i {q_interface} -s 0 -U -w {q_pcap} 'port 5060' >{q_log} 2>&1 & "
            f"pid=$!; echo \"$pid\" > {q_pid}; sleep 1; kill -0 \"$pid\" 2>/dev/null"
        )
        started_at = _utcnow()
        await mutator.execute_fenced(token, body=start_body)
        try:
            if trigger_command.strip():
                await mutator.execute_fenced(token, body=trigger_command)
            await asyncio.sleep(float(phase_seconds))
        finally:
            stop_body = (
                f"test -r {q_pid}; pid=$(cat {q_pid}); "
                "case \"$pid\" in ''|*[!0-9]*) exit 82;; esac; "
                "if kill -0 \"$pid\" 2>/dev/null; then kill -INT \"$pid\"; fi; "
                "i=0; while kill -0 \"$pid\" 2>/dev/null && [ \"$i\" -lt 20 ]; do "
                "sleep 0.25; i=$((i+1)); done; "
                "kill -0 \"$pid\" 2>/dev/null && exit 83; "
                f"test -s {q_pcap}"
            )
            await mutator.execute_fenced(token, body=stop_body)

        local_pcap = paths.dut_dir / f"{safe_phase}.pcap"
        await self.adapter.scp_get(remote_pcap, str(local_pcap), timeout=max(60.0, phase_seconds + 30.0))
        analysis = PacketIntelligenceEngine().analyze_pcap(local_pcap)
        ended_at = _utcnow()
        await mutator.execute_fenced(token, body=f"rm -f {q_pcap} {q_pid} {q_log}")
        return PhaseEvidence(
            phase=phase,
            pcap=str(local_pcap),
            pcap_sha256=_sha256(local_pcap),
            analysis=analysis,
            started_at=started_at,
            ended_at=ended_at,
        )

    async def _counter(self, rule: FirewallRule) -> int:
        result = await self.adapter.execute_shell(rule.counter_command(), retries=2)
        status = int(result.exit_status or 0)
        if status != 0:
            raise CaptureV2Error("SIP_ABA_RULE_COUNTER_UNAVAILABLE", details={"exit_status": status})
        values = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if len(values) != 1 or not values[0].isdigit():
            raise CaptureV2Error("SIP_ABA_RULE_COUNTER_AMBIGUOUS", details={"values": values})
        return int(values[0])

    async def _cleanup_rule(self, *, token, mutator, rule: FirewallRule) -> None:
        status = await self._exec_status(rule.check_command())
        if status == 0:
            await mutator.execute_fenced(token, body=rule.delete_command())
        if await self._exec_status(rule.check_command()) == 0:
            raise CaptureV2Error(
                "SIP_ABA_CLEANUP_FAILED",
                details={"rule_comment": rule.comment, "registrar": f"{rule.registrar_ip}:{rule.registrar_port}"},
            )

    async def run(
        self,
        *,
        device: GateDeviceSpec,
        reproduction_session_id: str,
        worker_id: str,
        gate_id: str = "SIP-REGISTRATION-ABA",
        phase_seconds: float = 20.0,
        transport: str = "udp",
        trigger_command: str = "",
        serial_command: str,
        software_version_command: str,
        allow_live_mutation: bool = False,
    ) -> GateCaseResult:
        if not allow_live_mutation:
            raise CaptureV2Error("SIP_ABA_EXPLICIT_LIVE_AUTH_REQUIRED")
        if str(os.getenv("REAL_LIVE_MUTATION", "")).strip() != "EXPLICIT_ONLY":
            raise CaptureV2Error(
                "SIP_ABA_REAL_LIVE_MUTATION_POLICY_BLOCKED",
                details={"required": "REAL_LIVE_MUTATION=EXPLICIT_ONLY"},
            )
        if not 3 <= float(phase_seconds) <= 300:
            raise CaptureV2Error("SIP_ABA_PHASE_SECONDS_INVALID")
        if transport not in {"udp", "tcp"}:
            raise CaptureV2Error("SIP_ABA_TRANSPORT_INVALID")

        paths = GateRunPaths.create(self.output_root, gate_id, device.device_id)
        evidence_path = paths.case_dir / "sip_registration_aba.json"
        a1_invariants = await self._read_invariants(
            device=device,
            serial_command=serial_command,
            software_version_command=software_version_command,
        )
        supervisor, capture_session_id = self._new_capture_session(
            reproduction_session_id=reproduction_session_id,
            device=device,
        )
        lease_manager = supervisor.lease_manager
        mutator = supervisor.mutator
        token = lease_manager.acquire(
            device_id=device.device_id,
            capture_session_id=capture_session_id,
            owner_worker_id=worker_id,
        )
        await mutator.publish_fence(token, boot_id=await self.reader.boot_id())

        rule: FirewallRule | None = None
        rule_applied = False
        phases: dict[str, PhaseEvidence] = {}
        checks: list[GateCheck] = []
        cleanup_error: Exception | None = None
        try:
            token = lease_manager.renew(token)
            phases["A1"] = await self._capture_phase(
                phase="A1", token=token, mutator=mutator,
                interface=a1_invariants["voice.interface"], phase_seconds=phase_seconds,
                trigger_command=trigger_command, paths=paths,
            )
            registrar_ip, registrar_port, a1_registration = select_healthy_registrar(phases["A1"].analysis)
            checks.append(GateCheck(
                name="A1_REGISTER_SUCCESS", passed=True,
                expected="exactly one healthy registrar with REGISTER -> 2xx",
                observed=f"{registrar_ip}:{registrar_port}",
                details={"final_status_code": a1_registration.get("final_status_code")},
            ))

            rule = FirewallRule(
                registrar_ip=registrar_ip,
                registrar_port=registrar_port,
                transport=transport,
                comment=f"AIVOIP_SIP_ABA_{capture_session_id[:8]}",
            )
            token = lease_manager.renew(token)
            await mutator.execute_fenced(token, body=rule.insert_command())
            rule_applied = True
            if await self._exec_status(rule.check_command()) != 0:
                raise CaptureV2Error("SIP_ABA_RULE_INSERT_VERIFY_FAILED")
            before_counter = await self._counter(rule)

            phases["B"] = await self._capture_phase(
                phase="B", token=token, mutator=mutator,
                interface=a1_invariants["voice.interface"], phase_seconds=phase_seconds,
                trigger_command=trigger_command, paths=paths,
            )
            after_counter = await self._counter(rule)
            hit_count = after_counter - before_counter
            b_has_success = registrar_success_observed(
                phases["B"].analysis, registrar_ip=registrar_ip, registrar_port=registrar_port,
            )
            checks.extend([
                GateCheck(
                    name="B_RULE_HIT", passed=hit_count > 0,
                    expected=">0 packets hit exact registrar DROP rule", observed=hit_count,
                    details={"before": before_counter, "after": after_counter, "rule_comment": rule.comment},
                ),
                GateCheck(
                    name="B_REGISTER_SUCCESS_ABSENT", passed=not b_has_success,
                    expected=False, observed=b_has_success,
                    details={"registrar": f"{registrar_ip}:{registrar_port}"},
                ),
            ])
            if hit_count <= 0:
                raise CaptureV2Error("SIP_ABA_FAULT_NOT_EXERCISED")
            if b_has_success:
                raise CaptureV2Error("SIP_ABA_B_PHASE_DID_NOT_FAIL")

            token = lease_manager.renew(token)
            await self._cleanup_rule(token=token, mutator=mutator, rule=rule)
            rule_applied = False
            checks.append(GateCheck(
                name="B_TO_A2_EXACT_CLEANUP", passed=True,
                expected="exact DROP rule absent before A2", observed="ABSENT",
                details={"rule_comment": rule.comment},
            ))

            phases["A2"] = await self._capture_phase(
                phase="A2", token=token, mutator=mutator,
                interface=a1_invariants["voice.interface"], phase_seconds=phase_seconds,
                trigger_command=trigger_command, paths=paths,
            )
            a2_success = registrar_success_observed(
                phases["A2"].analysis, registrar_ip=registrar_ip, registrar_port=registrar_port,
            )
            checks.append(GateCheck(
                name="A2_REGISTER_RECOVERED", passed=a2_success,
                expected=True, observed=a2_success,
                details={"registrar": f"{registrar_ip}:{registrar_port}"},
            ))
            if not a2_success:
                raise CaptureV2Error("SIP_ABA_A2_RECOVERY_NOT_OBSERVED")

            a2_invariants = await self._read_invariants(
                device=device,
                serial_command=serial_command,
                software_version_command=software_version_command,
            )
            invariant_keys = (
                "device.serial", "device.id", "software.version",
                "voice.voice_vlan_id", "voice.gateway_ip", "voice.interface",
            )
            drift = {
                key: {"A1": a1_invariants.get(key), "A2": a2_invariants.get(key)}
                for key in invariant_keys if a1_invariants.get(key) != a2_invariants.get(key)
            }
            checks.append(GateCheck(
                name="A1_A2_ENVIRONMENT_INVARIANTS", passed=not drift,
                expected="no drift", observed=drift or "EQUAL",
            ))
            if drift:
                raise CaptureV2Error("SIP_ABA_ENVIRONMENT_DRIFT", details={"drift": drift})

            payload = {
                "schema_version": 1,
                "profile_id": PROFILE_ID,
                "gate_id": gate_id,
                "verdict": "PASS",
                "causal_confirmation": "CONFIRMED",
                "device_id": device.device_id,
                "capture_session_id": capture_session_id,
                "reproduction_session_id": reproduction_session_id,
                "registrar": {"ip": registrar_ip, "port": registrar_port, "transport": transport},
                "fault": {
                    "scope": "DUT_LOCAL_OUTPUT_ONLY",
                    "rule_comment": rule.comment,
                    "hit_count": hit_count,
                    "persistent": False,
                    "pbx_mutated": False,
                    "default_route_mutated": False,
                },
                "A1_invariants": a1_invariants,
                "A2_invariants": a2_invariants,
                "phases": {name: row.as_dict() for name, row in phases.items()},
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "expected": check.expected,
                        "observed": check.observed,
                        "details": check.details,
                    }
                    for check in checks
                ],
                "created_at": _utcnow(),
            }
            evidence_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return GateCaseResult(
                gate_id=gate_id,
                verdict=GateVerdict.PASS,
                checks=tuple(checks),
                summary="Real DUT SIP registration A-B-A causal gate passed",
                evidence_bundle=str(paths.case_dir),
                facts={
                    "profile_id": PROFILE_ID,
                    "capture_session_id": capture_session_id,
                    "registrar": f"{registrar_ip}:{registrar_port}/{transport}",
                    "causal_confirmation": "CONFIRMED",
                    "evidence_json": str(evidence_path),
                },
            )
        finally:
            if rule is not None and rule_applied:
                try:
                    token = lease_manager.renew(token)
                    await self._cleanup_rule(token=token, mutator=mutator, rule=rule)
                except Exception as exc:
                    cleanup_error = exc
            try:
                token = lease_manager.renew(token)
                await mutator.release_fence(token)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            try:
                lease_manager.release(token)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                if isinstance(cleanup_error, CaptureV2Error):
                    raise cleanup_error
                raise CaptureV2Error(
                    "SIP_ABA_INFRASTRUCTURE_CLEANUP_FAILED",
                    details={"exception": type(cleanup_error).__name__},
                ) from cleanup_error

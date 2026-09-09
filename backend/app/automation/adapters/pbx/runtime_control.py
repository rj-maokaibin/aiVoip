from __future__ import annotations

import re
import uuid as uuidlib
from dataclasses import dataclass
from typing import Protocol

from app.automation.adapters.pbx.esl import FreeSwitchEventSocketClient, FreeSwitchEslError
from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    normalize_dial_alias,
    normalize_sip_user_wire_identity,
)
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import PbxLeaseToken

_DOMAIN_RE = re.compile(r"^[0-9A-Za-z._:-]{1,255}$")
_PROFILE_RE = re.compile(r"^[0-9A-Za-z_.+-]{1,64}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_DTMF_RE = re.compile(r"^[0-9A-Da-d*#]{1,32}$")
_RTP_VARS = (
    "rtp_audio_in_packet_count",
    "rtp_audio_out_packet_count",
    "rtp_audio_in_media_bytes",
    "rtp_audio_out_media_bytes",
)


class RuntimeAuthority(Protocol):
    def validate(self, token: PbxLeaseToken) -> bool: ...
    def validate_dial_alias_binding(self, token: PbxLeaseToken, dial_alias: str) -> bool: ...

class FreeSwitchRuntimeControlError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FreeSwitchOriginateResult:
    call_uuid: str
    job_uuid: str
    dial_alias: str
    domain_name: str

    def safe_dict(self) -> dict[str, object]:
        return {
            "call_uuid": self.call_uuid,
            "job_uuid": self.job_uuid,
            "dial_alias": self.dial_alias,
            "domain_name": self.domain_name,
            "mutation": True,
            "secret_values_emitted": False,
        }


class FreeSwitchRuntimeProvider:
    """Lease-fenced FreeSWITCH call/runtime mutation over a bounded command set."""

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        authority: RuntimeAuthority,
        client: FreeSwitchEventSocketClient | None = None,
    ) -> None:
        self.profile = profile
        self.authority = authority
        self.client = client or FreeSwitchEventSocketClient(profile, timeout_seconds=5.0)
    def connect(self) -> None:
        self.client.connect()

    def close(self) -> None:
        self.client.close()

    def _validate_token(self, token: PbxLeaseToken, *, dial_alias: str | None = None) -> None:
        if not self.authority.validate(token):
            raise FreeSwitchRuntimeControlError("PBX_CALL_LEASE_FENCED")
        if dial_alias is not None and not self.authority.validate_dial_alias_binding(token, dial_alias):
            raise FreeSwitchRuntimeControlError("PBX_CALL_DIAL_ALIAS_LEASE_FENCED")

    @staticmethod
    def _uuid(value: str) -> str:
        value = str(value or "").strip()
        if not _UUID_RE.fullmatch(value):
            raise FreeSwitchRuntimeControlError("PBX_CALL_UUID_INVALID")
        return value

    @staticmethod
    def _domain(value: str) -> str:
        value = str(value or "").strip()
        if not _DOMAIN_RE.fullmatch(value):
            raise FreeSwitchRuntimeControlError("PBX_CALL_DOMAIN_INVALID")
        return value

    def originate_alias(
        self,
        token: PbxLeaseToken,
        *,
        dial_alias: str,
        domain_name: str,
        tone_ms: int = 3000,
        tone_hz: int = 440,
    ) -> FreeSwitchOriginateResult:
        try:
            alias = normalize_dial_alias(dial_alias)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeControlError("PBX_CALL_DIAL_ALIAS_INVALID") from exc
        self._validate_token(token, dial_alias=alias)
        domain = self._domain(domain_name)
        duration, frequency = int(tone_ms), int(tone_hz)
        if not (500 <= duration <= 10000) or not (200 <= frequency <= 2000):
            raise FreeSwitchRuntimeControlError("PBX_CALL_MEDIA_PROFILE_INVALID")
        call_uuid = str(uuidlib.uuid4())
        command = (
            f"originate {{origination_uuid={call_uuid},ignore_early_media=true}}"
            f"user/{alias}@{domain} &playback(tone_stream://%({duration},0,{frequency}))"
        )
        try:
            job_uuid = self.client.bgapi(command)
        except FreeSwitchEslError as exc:
            raise FreeSwitchRuntimeControlError("PBX_CALL_ORIGINATE_FAILED") from exc
        return FreeSwitchOriginateResult(call_uuid, job_uuid, alias, domain)

    def inject_received_dtmf(self, token: PbxLeaseToken, call_uuid: str, digits: str) -> None:
        self._validate_token(token)
        uid = self._uuid(call_uuid)
        value = str(digits or "")
        if not _DTMF_RE.fullmatch(value):
            raise FreeSwitchRuntimeControlError("PBX_CALL_DTMF_INVALID")
        try:
            response = self.client.api(f"uuid_recv_dtmf {uid} {value}").strip()
        except FreeSwitchEslError as exc:
            raise FreeSwitchRuntimeControlError("PBX_CALL_DTMF_FAILED") from exc
        if response.startswith("-ERR"):
            raise FreeSwitchRuntimeControlError("PBX_CALL_DTMF_FAILED")

    def hangup(self, token: PbxLeaseToken, call_uuid: str) -> None:
        self._validate_token(token)
        uid = self._uuid(call_uuid)
        try:
            response = self.client.api(f"uuid_kill {uid} NORMAL_CLEARING").strip()
        except FreeSwitchEslError as exc:
            raise FreeSwitchRuntimeControlError("PBX_CALL_HANGUP_FAILED") from exc
        if response.startswith("-ERR") and "No such channel" not in response:
            raise FreeSwitchRuntimeControlError("PBX_CALL_HANGUP_FAILED")
    def rtp_stats(self, token: PbxLeaseToken, call_uuid: str) -> dict[str, int | bool]:
        self._validate_token(token)
        uid = self._uuid(call_uuid)
        values: dict[str, int | bool] = {"secret_values_emitted": False}
        for name in _RTP_VARS:
            try:
                output = self.client.api(f"uuid_getvar {uid} {name}").strip()
            except FreeSwitchEslError as exc:
                raise FreeSwitchRuntimeControlError("PBX_CALL_RTP_OBSERVE_FAILED") from exc
            if output.isdigit():
                values[name] = int(output)
        return values

    def flush_registration(
        self,
        token: PbxLeaseToken,
        *,
        identity: str,
        domain_name: str,
    ) -> None:
        self._validate_token(token)
        try:
            user = normalize_sip_user_wire_identity(identity)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRuntimeControlError("PBX_REGISTRATION_IDENTITY_INVALID") from exc
        domain = self._domain(domain_name)
        profile_name = str(self.profile.internal_profile or "").strip()
        if not _PROFILE_RE.fullmatch(profile_name):
            raise FreeSwitchRuntimeControlError("PBX_RUNTIME_PROFILE_INVALID")
        try:
            response = self.client.api(
                f"sofia profile {profile_name} flush_inbound_reg {user}@{domain}"
            ).strip()
        except FreeSwitchEslError as exc:
            raise FreeSwitchRuntimeControlError("PBX_REGISTRATION_CLEANUP_FAILED") from exc
        if response.startswith("-ERR"):
            raise FreeSwitchRuntimeControlError("PBX_REGISTRATION_CLEANUP_FAILED")

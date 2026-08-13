from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlatformProfileStatus(StrEnum):
    DRAFT = 'DRAFT'
    PARTIAL = 'PARTIAL'
    VERIFIED = 'VERIFIED'
    DISABLED = 'DISABLED'


class ResolverContract(BaseModel):
    """Declarative resolver contract.

    `command_action_id` must point at an already registered read-only Action.  Parser details
    remain explicit so an implementation cannot silently infer a field from arbitrary text.
    A resolver with `parser_status=RESERVED` is documentation only and MUST NOT be used by
    production autonomous reproduction.
    """

    model_config = ConfigDict(extra='forbid')

    command_action_id: str | None = None
    parser_id: str | None = None
    parser_status: str = 'RESERVED'  # RESERVED | PROVISIONAL | VERIFIED
    cardinality: str | None = None
    derive: str | None = None
    verification_action_id: str | None = None
    notes: str = ''


class ContractGap(BaseModel):
    model_config = ConfigDict(extra='forbid')

    key: str
    severity: str = 'BLOCKING'
    blocking_capabilities: list[str] = Field(default_factory=list)
    description: str
    required_confirmation: str


class TransportContract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    ssh_username: str = 'admin'
    aim_executable: str = 'aim'
    aim_root_prompt: str = 'AIM>'
    aim_session: str = 'PERSISTENT_PTY'


class KnownDiagnosticTemplate(BaseModel):
    """A source-backed command shape that is intentionally not executable yet.

    This exists to preserve facts such as the known PCM start syntax without registering an
    Action that could be executed before cleanup and verification semantics are frozen.
    """

    model_config = ConfigDict(extra='forbid')

    template_id: str
    executor: str
    command_template: str
    status: str = 'DOCUMENTED_ONLY'
    reason_not_activatable: str
    cleanup_command_template: str | None = None
    cleanup_status: str = 'UNCONFIRMED'
    cleanup_idempotent: bool | None = None
    cleanup_retry_strategy: str = 'UNSPECIFIED'
    cleanup_guard: str | None = None
    # FXS / sub-mode prompt contract: entering the submode yields a distinct prompt
    # and a read-only snapshot command is available. Kept optional for compat.
    submode_prompt: str | None = None
    snapshot_command: str | None = None
    snapshot_fields: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class PlatformProfileDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    name: str
    version: str
    status: PlatformProfileStatus
    family: str
    description: str = ''
    transport: TransportContract = Field(default_factory=TransportContract)
    source_refs: list[str] = Field(default_factory=list)
    readonly_actions: list[str] = Field(default_factory=list)
    autonomous_reproduction_actions: list[str] = Field(default_factory=list)
    voice_runtime_context: dict[str, ResolverContract] = Field(default_factory=dict)
    realtime_event_sources: dict[str, ResolverContract] = Field(default_factory=dict)
    known_diagnostic_templates: list[KnownDiagnosticTemplate] = Field(default_factory=list)
    gaps: list[ContractGap] = Field(default_factory=list)

    @model_validator(mode='after')
    def verified_cannot_have_blocking_gaps(self):
        if self.status == PlatformProfileStatus.VERIFIED:
            blocking = [g.key for g in self.gaps if g.severity == 'BLOCKING']
            if blocking:
                raise ValueError(f'VERIFIED_PLATFORM_HAS_BLOCKING_GAPS:{blocking}')
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude_none=True)

    def checksum(self) -> str:
        raw = json.dumps(self.canonical(), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def blocking_gaps_for(self, capability: str) -> list[ContractGap]:
        return [
            gap for gap in self.gaps
            if gap.severity == 'BLOCKING' and capability in gap.blocking_capabilities
        ]

    def production_ready_for(self, capability: str) -> bool:
        return self.status == PlatformProfileStatus.VERIFIED and not self.blocking_gaps_for(capability)

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.enums import CaptureChannel, CaptureStage, EndPolicy, ReproductionProfileStatus
from app.core.config import settings
from app.reproduction.contracts import REPRODUCTION_ACTION_IDS, SAFE_AUTO_ARM_ACTION_IDS, CLEANUP_ACTION_IDS, ACTION_CLEANUP_PAIR
from app.actions.registry import ActionRegistry, RegistryError


class StageConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: CaptureStage
    auto_arm_actions: list[str]
    required_channels: list[CaptureChannel]
    live_analyzers: list[str] = Field(default_factory=list)
    quick_analyzers: list[str] = Field(default_factory=list)


class RetryPolicyConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_sessions: int = Field(default=2, ge=1, le=20)
    interval_seconds: int = Field(default=30, ge=0, le=86400)
    retryable_reasons: list[str] = Field(default_factory=lambda: ['WATCH_TIMEOUT','TRANSIENT_DEVICE_LOST'])


class TimeoutsConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    arm_timeout_seconds: int = Field(default=20, ge=1, le=600)
    watching_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    max_capture_seconds: int = Field(default=900, ge=1, le=86400)
    post_capture_seconds: int = Field(default=3, ge=0, le=120)
    cleanup_timeout_seconds: int = Field(default=20, ge=1, le=600)
    cleanup_quiet_seconds: int = Field(default=2, ge=0, le=60)
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    heartbeat_seconds: int = Field(default=15, ge=1, le=600)




class ArmBarrierConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # min_* may be 0 for real DUTs where arm readiness means "capture facility
    # ready" (channels armed, probes listening) rather than "live traffic already
    # flowing" ¡ª real media only appears once an FXS event triggers a call.
    min_pcap_packets: int = Field(default=2, ge=0, le=10000)
    min_pcm_packets: int = Field(default=3, ge=0, le=10000)
    require_advancing: bool = True
    retry_attempts: int = Field(default=2, ge=1, le=10)

class RingConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pretrigger_seconds: int = Field(default=30, ge=1, le=600)
    segment_seconds: int = Field(default=5, ge=1, le=60)
    max_session_size_mb: int = Field(default=2048, ge=32, le=102400)


class SufficiencyConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question_key: str
    require_target_match: bool = True
    must_channels: list[CaptureChannel]
    must_findings: list[str] = Field(default_factory=list)
    should_findings: list[str] = Field(default_factory=list)
    require_control_target_pair: bool = False
    require_no_hard_contradiction: bool = True


class ReproductionProfileDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    name: str
    version: str
    status: ReproductionProfileStatus = ReproductionProfileStatus.ACTIVE
    symptom_classes: list[str]
    description: str = ''
    primary_start_anchor: str
    call_binding_event: str = 'SIP_INVITE'
    media_binding_event: str = 'RTP_STREAM_START'
    primary_end_anchor: str = 'FXS_ONHOOK'
    secondary_end_anchor: str = 'SIP_BYE'
    fallback_end_anchor: str = 'RTP_IDLE'
    end_policy: EndPolicy = EndPolicy.EVIDENCE_SUFFICIENCY
    max_calls: int = Field(default=5, ge=1, le=100)
    target_count: int = Field(default=1, ge=1, le=100)
    allow_partial_capability_downgrade: bool = False
    stages: list[StageConfig]
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    ring: RingConfig = Field(default_factory=RingConfig)
    retry: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)
    arm_barrier: ArmBarrierConfig = Field(default_factory=ArmBarrierConfig)
    sufficiency: SufficiencyConfig
    cleanup_actions: list[str]

    @model_validator(mode='after')
    def validate_contract(self):
        stage_names=[s.stage for s in self.stages]
        if CaptureStage.BASE not in stage_names:
            raise ValueError('BASE_STAGE_REQUIRED')
        if len(set(stage_names)) != len(stage_names):
            raise ValueError('DUPLICATE_CAPTURE_STAGE')
        if not self.cleanup_actions:
            raise ValueError('CLEANUP_ACTIONS_REQUIRED')
        all_arm=[a for stage in self.stages for a in stage.auto_arm_actions]
        unknown=[a for a in all_arm + self.cleanup_actions if a not in REPRODUCTION_ACTION_IDS]
        if unknown:
            raise ValueError(f'UNKNOWN_REPRODUCTION_ACTION:{unknown}')
        unsafe=[a for a in all_arm if a not in SAFE_AUTO_ARM_ACTION_IDS]
        if unsafe:
            raise ValueError(f'UNSAFE_AUTO_ARM_ACTION:{unsafe}')
        invalid_cleanup=[a for a in self.cleanup_actions if a not in CLEANUP_ACTION_IDS]
        if invalid_cleanup:
            raise ValueError(f'INVALID_CLEANUP_ACTION:{invalid_cleanup}')
        required_cleanup={ACTION_CLEANUP_PAIR[a] for a in all_arm if a in ACTION_CLEANUP_PAIR}
        missing_cleanup=sorted(required_cleanup - set(self.cleanup_actions))
        if missing_cleanup:
            raise ValueError(f'ARM_CLEANUP_ASYMMETRY:{missing_cleanup}')
        if self.end_policy == EndPolicy.CONTROL_TARGET_PAIR:
            self.sufficiency.require_control_target_pair = True
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude_none=True)

    def checksum(self) -> str:
        raw=json.dumps(self.canonical(), ensure_ascii=False, sort_keys=True, separators=(',',':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class LoadedReproductionProfile:
    definition: ReproductionProfileDefinition
    checksum: str
    source_path: Path


class ReproductionProfileRegistryError(RuntimeError):
    pass


class ReproductionProfileRegistry:
    def __init__(self, root: Path | None = None):
        base=root or settings.profile_root
        if not Path(base).exists():
            base=Path(__file__).resolve().parents[3] / 'profiles'
        self.root=Path(base) / 'reproduction'
        self._profiles: dict[str, LoadedReproductionProfile] = {}
        self.reload()

    def reload(self):
        profiles: dict[str, LoadedReproductionProfile] = {}
        if not self.root.exists():
            self._profiles = {}
            return
        for path in sorted(self.root.glob('*.yaml')):
            doc=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            for raw in doc.get('profiles', []):
                definition=ReproductionProfileDefinition.model_validate(raw)
                if definition.id in profiles:
                    raise ReproductionProfileRegistryError(f'DUPLICATE_REPRODUCTION_PROFILE:{definition.id}')
                if definition.status not in {ReproductionProfileStatus.APPROVED, ReproductionProfileStatus.ACTIVE}:
                    continue
                profiles[definition.id]=LoadedReproductionProfile(definition, definition.checksum(), path)
        # Cross-check abstract actions against the central Action Registry. They are registered as executor=mock,
        # therefore Phase C cannot accidentally execute a real shell/AIM command.
        try:
            action_registry=ActionRegistry(self.root.parent)
            for loaded in profiles.values():
                d=loaded.definition
                for action_id in [a for stage in d.stages for a in stage.auto_arm_actions] + d.cleanup_actions:
                    action=action_registry.action(action_id)
                    if action.executor != 'mock':
                        raise ReproductionProfileRegistryError(f'REPRODUCTION_ACTION_NOT_MOCK:{action_id}')
        except (RegistryError, FileNotFoundError) as exc:
            raise ReproductionProfileRegistryError(f'ACTION_REGISTRY_VALIDATION_FAILED:{exc}') from exc
        self._profiles=profiles

    def get(self, profile_id: str) -> LoadedReproductionProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ReproductionProfileRegistryError(f'UNKNOWN_REPRODUCTION_PROFILE:{profile_id}') from exc

    def list(self) -> list[LoadedReproductionProfile]:
        return [self._profiles[k] for k in sorted(self._profiles)]

    def select_for_symptom(self, symptom_class: str | None) -> LoadedReproductionProfile:
        if symptom_class:
            normalized=symptom_class.upper()
            for loaded in self.list():
                if normalized in {x.upper() for x in loaded.definition.symptom_classes}:
                    return loaded
        return self.get('VOIP_GENERIC_FULL_CAPTURE')

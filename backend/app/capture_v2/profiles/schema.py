from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.capture_v2.enums import ChannelRequirement


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["FULL_VOICE"] = "FULL_VOICE"
    snaplen: Literal[0] = 0
    segment_seconds: Literal[5] = 5


class TransferConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["SFTP"] = "SFTP"
    parallelism: int = Field(default=1, ge=1, le=8)
    server_sha256: Literal[True] = True
    remote_sha256: bool = False
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)


class ChannelRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pcap: Literal[ChannelRequirement.REQUIRED] = ChannelRequirement.REQUIRED
    fxs: Literal[ChannelRequirement.REQUIRED] = ChannelRequirement.REQUIRED
    pcm_rx: Literal[ChannelRequirement.CONDITIONAL_REQUIRED] = ChannelRequirement.CONDITIONAL_REQUIRED
    pcm_tx: Literal[ChannelRequirement.CONDITIONAL_REQUIRED] = ChannelRequirement.CONDITIONAL_REQUIRED
    debug: ChannelRequirement = ChannelRequirement.OPTIONAL


class LeaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ttl_seconds: float = Field(default=30.0, ge=10.0, le=300.0)
    renew_interval_seconds: float = Field(default=10.0, ge=2.0, le=120.0)

    @model_validator(mode="after")
    def validate_renew_before_expiry(self):
        if self.ttl_seconds <= self.renew_interval_seconds * 2:
            raise ValueError("CAPTURE_LEASE_TTL_TOO_SHORT")
        return self


class SpoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_oldest_unacked_seconds: float | None = Field(default=None, ge=1.0)
    pressure_policy: Literal["FAIL_CLOSED_NO_EVICT_UNACKED"] = "FAIL_CLOSED_NO_EVICT_UNACKED"


class FxsSemanticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hook_glitch_max_ms: int = Field(default=100, ge=10, le=1000)
    post_onhook_rebound_window_ms: int = Field(default=500, ge=20, le=5000)
    stable_offhook_confirm_ms: int = Field(default=100, ge=20, le=2000)
    # Software defaults only; must be calibrated by the deferred real Hook-Flash Gate.
    hook_flash_min_ms: int = Field(default=100, ge=20, le=2000)
    hook_flash_max_ms: int = Field(default=1000, ge=50, le=5000)

    @model_validator(mode="after")
    def validate_flash_window(self):
        if self.hook_flash_max_ms <= self.hook_flash_min_ms:
            raise ValueError("hook_flash_max_ms must be greater than hook_flash_min_ms")
        return self


class ReadinessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    watchdog_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    pcm_readiness_timeout_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    sip_expectation_timeout_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    rtp_expectation_timeout_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    pcm_media_expectation_timeout_seconds: float = Field(default=2.0, ge=0.5, le=60.0)


class CoverageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pre_trigger_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
    post_trigger_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
    possible_gap_downgrades_complete: Literal[True] = True


class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_version: str = "capture-quality-v2.1"
    confidence_requires_complete_capture: bool = True


class LifecycleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    post_target_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
    evidence_finalize_timeout_seconds: float = Field(default=120.0, ge=5.0, le=1800.0)


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_rolling_retention_seconds: float = Field(default=300.0, ge=5.0, le=86400.0)


class CaptureProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: str = Field(min_length=1, max_length=64)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    transfer: TransferConfig = Field(default_factory=TransferConfig)
    channels: ChannelRequirements = Field(default_factory=ChannelRequirements)
    lease: LeaseConfig = Field(default_factory=LeaseConfig)
    spool: SpoolConfig = Field(default_factory=SpoolConfig)
    fxs: FxsSemanticConfig = Field(default_factory=FxsSemanticConfig)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class PlatformResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_transfer_parallelism: int = Field(default=1, ge=1, le=8)
    spool_max_unacked_bytes: int | None = Field(default=None, ge=1)
    spool_min_free_bytes: int | None = Field(default=None, ge=1)


class PlatformProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    platform_id: Literal["mt7621", "mt7981"]
    profile_version: str = Field(min_length=1, max_length=64)
    models: tuple[str, ...] = ()
    resource: PlatformResourceConfig = Field(default_factory=PlatformResourceConfig)


class EffectiveCaptureProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capture_profile_id: str
    capture_profile_version: str
    platform_profile_id: str
    platform_profile_version: str
    resolved: dict
    checksum_sha256: str

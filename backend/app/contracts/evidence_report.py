from __future__ import annotations

from enum import StrEnum


REPORT_SCHEMA_VERSION = "preliminary-evidence-report-v1"
REPORT_COMPOSER_VERSION = "evidence-brief-composer-v4"
FINDING_SIGNATURE_VERSION = "sig-v1"
RENDERER_VERSION = "evidence-renderer-v2"
EVIDENCE_CARD_VERSION = "evidence-card-v1"
GROUNDING_VALIDATOR_VERSION = "report-grounding-v1"
CLAIM_MANIFEST_VERSION = "report-claim-manifest-v1"


class EvidenceReportScope(StrEnum):
    CALL = "CALL"
    SESSION = "SESSION"
    CASE = "CASE"


class AnalysisMode(StrEnum):
    REPRODUCTION = "REPRODUCTION"
    OFFLINE_IMPORTED = "OFFLINE_IMPORTED"


class CallOrigin(StrEnum):
    REPRODUCTION_RUNTIME = "REPRODUCTION_RUNTIME"
    RECONSTRUCTED_FROM_PCAP = "RECONSTRUCTED_FROM_PCAP"
    MEDIA_SESSION_UNBOUND = "MEDIA_SESSION_UNBOUND"


class CallScope(StrEnum):
    BOUND = "BOUND"
    UNBOUND = "UNBOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceReportStatus(StrEnum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    COMPOSING = "COMPOSING"
    COMPLETE = "COMPLETE"
    PARTIAL_COMPLETE = "PARTIAL_COMPLETE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class EvidenceFindingStatus(StrEnum):
    PROPOSED = "PROPOSED"
    OBSERVED = "OBSERVED"
    PERSISTING = "PERSISTING"
    RESOLVED = "RESOLVED"
    REVISED = "REVISED"
    INVALIDATED = "INVALIDATED"


class EvidenceFindingSeverity(StrEnum):
    INFO = "INFO"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


SEVERITY_ORDER = {
    EvidenceFindingSeverity.INFO.value: 0,
    EvidenceFindingSeverity.MEDIUM.value: 1,
    EvidenceFindingSeverity.HIGH.value: 2,
    EvidenceFindingSeverity.CRITICAL.value: 3,
}

EVIDENCE_LEVEL_ORDER = {"L5": 0, "L4": 1, "L3": 2, "L2": 3, "L1": 4}


class EvidenceReportArtifactType(StrEnum):
    RAW_PCAP = "RAW_PCAP"
    PCM_WAV = "PCM_WAV"
    RTP_WAV = "RTP_WAV"
    AUDIO_CLIP = "AUDIO_CLIP"
    WAVEFORM_PNG = "WAVEFORM_PNG"
    SPECTRUM_PNG = "SPECTRUM_PNG"
    SPECTROGRAM_PNG = "SPECTROGRAM_PNG"
    RTP_TIMELINE_PNG = "RTP_TIMELINE_PNG"
    SIP_CALL_FLOW_PNG = "SIP_CALL_FLOW_PNG"
    PACKET_ANALYSIS_JSON = "PACKET_ANALYSIS_JSON"
    PCM_ANALYSIS_JSON = "PCM_ANALYSIS_JSON"
    MEDIA_ANALYSIS_JSON = "MEDIA_ANALYSIS_JSON"
    PRELIMINARY_REPORT_HTML = "PRELIMINARY_REPORT_HTML"
    PRELIMINARY_REPORT_JSON = "PRELIMINARY_REPORT_JSON"
    EVIDENCE_BUNDLE = "EVIDENCE_BUNDLE"
    MANIFEST_JSON = "MANIFEST_JSON"


# Frozen SPEC §28 concrete deterministic Finding types. Only types that are
# actually emitted by the current Analyzer/Composer are allowed here. Metric
# capabilities such as RFC3550 jitter and dBFS remain mandatory report facts,
# but are not promoted to a Finding until a calibrated anomaly threshold is
# frozen in AnalyzerProfile/Golden data; this prevents threshold invention.
P0_FINDING_TYPES = {
    "SIP_REGISTRATION_FAILED",
    "SIP_CALL_FAILED",
    "SIP_CONFLICTING_FINAL_RESPONSE",
    "CODEC_NEGOTIATION_MISMATCH",
    "ONE_WAY_RTP_MEDIA",
    "PACKET_LOSS",
    "BURST_LOSS",
    "HIGH_DELTA",
    "PAYLOAD_CHANGE",
    "PCM_GAP",
    "UNEXPECTED_SILENCE",
    "CLICK_POP",
    "PERIODIC_LOW_FREQUENCY_INTERFERENCE",
    "LOCAL_CAPTURE_PERIODIC_INTERFERENCE",
    "ECHO_PATH_DETECTED",
    "DTMF_ABNORMAL",
    "PERIODIC_INTERFERENCE_PATH_COMPARISON",
}

# Frozen §28 capabilities that are required as deterministic measurements even
# when V1.0 has no calibrated standalone abnormal-Finding threshold for them.
P0_MEASUREMENT_CAPABILITIES = {
    "RTP_RFC3550_JITTER",
    "RTP_PTIME",
    "PCM_RMS_DBFS",
    "PCM_PEAK_DBFS",
    "EVIDENCE_COMPLETENESS_7D",
}


DEFAULT_ROOT_CAUSE_BOUNDARY = (
    "这是当前 Case 的初步证据 Finding，不等于最终根因；"
    "具体根因仍需当前 Case L1/L2 直接证据、确定性确认规则、无关键反证及人工/修复验证。"
)

PERIODIC_ROOT_CAUSE_BOUNDARY = (
    "周期性低频/工频族特征只能说明数字音频中存在对应频域证据，"
    "不能单独确认电源、接地、话柄、SLIC 或其他物理硬件根因。"
)
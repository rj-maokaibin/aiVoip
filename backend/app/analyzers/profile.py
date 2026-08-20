from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml


class AnalyzerProfileError(ValueError):
    pass


_ALLOWED_STATUS = {"GOLDEN_CALIBRATED", "EXPERIMENTAL", "UNCALIBRATED"}
_REQUIRED_SECTIONS = {
    "rtp", "silence", "click_pop", "echo", "periodic", "spectral", "dtmf", "basic_metrics", "hum", "correlation"
}


@dataclass(frozen=True, slots=True)
class AnalyzerProfile:
    schema_version: int
    id: str
    version: str
    status: str
    config: dict[str, Any]
    checksum: str
    source_path: str

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.config.get(name)
        if not isinstance(value, dict):
            raise AnalyzerProfileError(f"ANALYZER_PROFILE_SECTION_MISSING:{name}")
        return value

    @property
    def confirmable(self) -> bool:
        return self.status == "GOLDEN_CALIBRATED"

    def metadata(self) -> dict[str, Any]:
        return {
            "profile_id": self.id,
            "profile_version": self.version,
            "profile_status": self.status,
            "profile_checksum": self.checksum,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "status": self.status,
            "checksum": self.checksum,
            "config": self.config,
        }


def _canonical_checksum(raw: dict[str, Any]) -> str:
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _positive(section: Mapping[str, Any], key: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProfileError(f"ANALYZER_PROFILE_INVALID_VALUE:{key}") from exc
    if value < 0 if allow_zero else value <= 0:
        raise AnalyzerProfileError(f"ANALYZER_PROFILE_OUT_OF_RANGE:{key}")
    return value


def _ratio(section: Mapping[str, Any], key: str) -> float:
    try:
        value = float(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyzerProfileError(f"ANALYZER_PROFILE_INVALID_VALUE:{key}") from exc
    if not 0.0 <= value <= 1.0:
        raise AnalyzerProfileError(f"ANALYZER_PROFILE_OUT_OF_RANGE:{key}")
    return value


def validate_analyzer_profile(raw: dict[str, Any]) -> None:
    if int(raw.get("schema_version", 0)) != 1:
        raise AnalyzerProfileError("ANALYZER_PROFILE_SCHEMA_UNSUPPORTED")
    if not str(raw.get("id") or "").strip():
        raise AnalyzerProfileError("ANALYZER_PROFILE_ID_REQUIRED")
    if not str(raw.get("version") or "").strip():
        raise AnalyzerProfileError("ANALYZER_PROFILE_VERSION_REQUIRED")
    status = str(raw.get("status") or "")
    if status not in _ALLOWED_STATUS:
        raise AnalyzerProfileError("ANALYZER_PROFILE_STATUS_INVALID")
    missing = sorted(_REQUIRED_SECTIONS - set(raw))
    if missing:
        raise AnalyzerProfileError(f"ANALYZER_PROFILE_SECTIONS_MISSING:{','.join(missing)}")

    rtp = raw["rtp"]
    if float(rtp["ptime_min_ms"]) >= float(rtp["ptime_max_ms"]):
        raise AnalyzerProfileError("ANALYZER_PROFILE_PTIME_RANGE_INVALID")
    _positive(rtp, "high_delta_multiplier")
    _positive(rtp, "jitter_filter_divisor")

    silence = raw["silence"]
    _positive(silence, "frame_ms")
    _positive(silence, "min_duration_ms")
    if float(silence["threshold_min_dbfs"]) >= float(silence["threshold_max_dbfs"]):
        raise AnalyzerProfileError("ANALYZER_PROFILE_SILENCE_THRESHOLD_RANGE_INVALID")

    click = raw["click_pop"]
    _positive(click, "min_jump")
    _positive(click, "mad_multiplier")
    _positive(click, "min_energy_rise_db", allow_zero=True)
    if not 0 <= float(click["min_highband_ratio"]) <= 1:
        raise AnalyzerProfileError("ANALYZER_PROFILE_CLICK_HIGHBAND_INVALID")

    candidate = raw.get("candidate_decision")
    if candidate is not None:
        if not isinstance(candidate, dict):
            raise AnalyzerProfileError("ANALYZER_PROFILE_CANDIDATE_DECISION_INVALID")
        _positive(candidate, "dtmf_guard_ms", allow_zero=True)
        _positive(candidate, "media_boundary_guard_ms", allow_zero=True)
        _ratio(candidate, "silence_counterpart_overlap_ratio")
        _ratio(candidate, "silence_min_correlation")
        _ratio(candidate, "silence_counterpart_active_ratio")
        _positive(candidate, "silence_counterpart_active_margin_db", allow_zero=True)

    echo = raw["echo"]
    if float(echo["min_delay_ms"]) >= float(echo["max_delay_ms"]):
        raise AnalyzerProfileError("ANALYZER_PROFILE_ECHO_DELAY_RANGE_INVALID")
    if not 0 <= float(echo["min_correlation"]) <= 1:
        raise AnalyzerProfileError("ANALYZER_PROFILE_ECHO_CORRELATION_INVALID")

    periodic = raw["periodic"]
    if float(periodic["period_search_min_ms"]) >= float(periodic["period_search_max_ms"]):
        raise AnalyzerProfileError("ANALYZER_PROFILE_PERIOD_RANGE_INVALID")
    for level in ("strong", "medium"):
        cfg = periodic[level]
        if not -1 <= float(cfg["ac10_max"]) <= 1:
            raise AnalyzerProfileError(f"ANALYZER_PROFILE_PERIOD_{level.upper()}_AC10_INVALID")
        if not -1 <= float(cfg["ac20_min"]) <= 1 or not -1 <= float(cfg["ac40_min"]) <= 1:
            raise AnalyzerProfileError(f"ANALYZER_PROFILE_PERIOD_{level.upper()}_AC_INVALID")

    dtmf = raw["dtmf"]
    _positive(dtmf, "frame_ms")
    _positive(dtmf, "hop_ms")
    if float(dtmf["min_duration_ms"]) < float(dtmf["frame_ms"]):
        raise AnalyzerProfileError("ANALYZER_PROFILE_DTMF_DURATION_INVALID")
    _positive(dtmf, "min_interdigit_gap_ms")
    quality = float(dtmf["quality_min_confidence"])
    if not 0.0 <= quality <= 1.0:
        raise AnalyzerProfileError("ANALYZER_PROFILE_DTMF_QUALITY_CONFIDENCE_INVALID")


def load_analyzer_profile(path: str | Path) -> AnalyzerProfile:
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AnalyzerProfileError("ANALYZER_PROFILE_DOCUMENT_INVALID")
    validate_analyzer_profile(raw)
    config = {k: v for k, v in raw.items() if k not in {"schema_version", "id", "version", "status", "description"}}
    return AnalyzerProfile(
        schema_version=int(raw["schema_version"]),
        id=str(raw["id"]),
        version=str(raw["version"]),
        status=str(raw["status"]),
        config=config,
        checksum=_canonical_checksum(raw),
        source_path=str(source),
    )


def default_analyzer_profile_path() -> Path:
    override = os.getenv("VOIP_ANALYZER_PROFILE")
    if override:
        return Path(override)
    runtime = Path("/app/profiles/analyzers/voip_v1.yaml")
    if runtime.exists():
        return runtime
    repo = Path(__file__).resolve().parents[3]
    return repo / "profiles" / "analyzers" / "voip_v1.yaml"


@lru_cache(maxsize=1)
def get_default_analyzer_profile() -> AnalyzerProfile:
    return load_analyzer_profile(default_analyzer_profile_path())

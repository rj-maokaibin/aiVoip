from __future__ import annotations

from app.capture_v2.coverage.calculator import EvidenceInterval
from app.capture_v2.coverage.ledger import CoverageLedgerService
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.r5_live_coverage import run_r5_real_live_coverage as _run_r5_real_live_coverage


_ORIGINAL_CALCULATE_TRACK = CoverageLedgerService.calculate_track


def _calculate_track_db_contract(
    self,
    *,
    coverage_window_id: str,
    channel: str,
    requirement: str,
    evidence: list[EvidenceInterval],
    applicable: bool = True,
    uncertain_boundary: bool = False,
):
    """Validation-only adapter for the persisted CoverageInterval schema.

    The R5 real-call gate originally used the descriptive source kind
    ``REAL_AIM_FXS_MONITOR_AVAILABILITY`` (33 chars) while the production DB
    contract is CoverageInterval.source_kind VARCHAR(32). Do not widen or weaken
    the production schema from a gate. Map only that validation marker to a stable
    <=32-char token and fail closed for any other overlong marker.
    """
    normalized: list[EvidenceInterval] = []
    for item in evidence:
        source_kind = str(item.source_kind)
        if source_kind == "REAL_AIM_FXS_MONITOR_AVAILABILITY":
            source_kind = "REAL_AIM_FXS_MONITOR"
        if len(source_kind) > 32:
            raise CaptureV2Error(
                "COVERAGE_SOURCE_KIND_DB_CONTRACT_VIOLATION",
                details={"source_kind": source_kind, "max_length": 32},
            )
        normalized.append(EvidenceInterval(
            start=item.start,
            end=item.end,
            interval_type=item.interval_type,
            source_kind=source_kind,
            source_id=item.source_id,
            certainty=item.certainty,
            details=item.details,
        ))
    return _ORIGINAL_CALCULATE_TRACK(
        self,
        coverage_window_id=coverage_window_id,
        channel=channel,
        requirement=requirement,
        evidence=normalized,
        applicable=applicable,
        uncertain_boundary=uncertain_boundary,
    )


async def run_r5_real_live_coverage(*args, **kwargs):
    original = CoverageLedgerService.calculate_track
    CoverageLedgerService.calculate_track = _calculate_track_db_contract
    try:
        return await _run_r5_real_live_coverage(*args, **kwargs)
    finally:
        CoverageLedgerService.calculate_track = original

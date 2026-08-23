from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.capture_v2.coverage.ledger import CoverageLedgerService
from app.capture_v2.coverage.pcap_source import PcapCoverageEvidenceBuilder
from app.capture_v2.db_models import (
    CaptureAttempt,
    CaptureEpoch,
    CaptureEvent,
    CaptureSegment,
    CaptureSession,
    CoverageTrack,
    CoverageWindow,
)
from app.capture_v2.enums import CoverageStatus
from app.capture_v2.f_bridge import CaptureV2FQualityReporter
from app.capture_v2.quality.signals import SignalEvidence
from app.capture_v2.report.evidence_first import EvidenceAssetRepository, FindingEvidenceRequest
from app.capture_v2.storage.local import LocalDurableSegmentStore
from app.core.config import settings
from app.db.session import SessionLocal


EXPECTED_GOLDEN_SCHEMA = "capture-v2-r6-abnormal-golden-v1"
EXPECTED_FINDING = "FIRST_DIGIT_8_MISSING_BY_AIM_FXS_DTMF_EVENT_LAYER"
DURABLE_SEGMENT_STATES = {"PERSISTED", "ACK_PENDING", "ACKED", "REMOTE_DELETED"}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _overlaps(start: datetime | None, end: datetime | None,
              required_start: datetime, required_end: datetime) -> bool:
    start = _utc(start)
    end = _utc(end)
    required_start = _utc(required_start)
    required_end = _utc(required_end)
    if start is None or required_start is None or required_end is None:
        return False
    effective_end = end or required_end
    if effective_end <= start:
        return required_start <= start < required_end
    return start < required_end and effective_end > required_start


def _load_golden(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != EXPECTED_GOLDEN_SCHEMA:
        raise RuntimeError("R6_GOLDEN_SCHEMA_INVALID")
    if str(((raw.get("finding") or {}).get("conclusion") or "")) != EXPECTED_FINDING:
        raise RuntimeError("R6_GOLDEN_FINDING_MISMATCH")
    if str(raw.get("expected_panel_target") or "") != "8000":
        raise RuntimeError("R6_GOLDEN_EXPECTED_TARGET_MISMATCH")
    if str(raw.get("observed_dtmf_digits") or "") != "000":
        raise RuntimeError("R6_GOLDEN_OBSERVED_DIGITS_MISMATCH")
    return raw


def _capture_facts(golden: dict[str, Any]) -> dict[str, Any]:
    capture_session_id = str(golden["capture_session_id"])
    capture_attempt_id = str(golden["capture_attempt_id"])
    coverage_window_id = str(golden["coverage_window_id"])
    with SessionLocal() as db:
        dialect = str(db.get_bind().dialect.name)
        capture = db.get(CaptureSession, capture_session_id)
        attempt = db.get(CaptureAttempt, capture_attempt_id)
        window = db.get(CoverageWindow, coverage_window_id)
        tracks = list(db.scalars(select(CoverageTrack).where(
            CoverageTrack.coverage_window_id == coverage_window_id
        )))
    if dialect != "postgresql":
        raise RuntimeError(f"R6_PRODUCT_REPORT_REAL_POSTGRES_REQUIRED:{dialect}")
    if capture is None:
        raise RuntimeError("R6_CAPTURE_SESSION_NOT_FOUND")
    if attempt is None or attempt.capture_session_id != capture_session_id:
        raise RuntimeError("R6_CAPTURE_ATTEMPT_NOT_FOUND_OR_MISMATCH")
    if window is None or window.capture_session_id != capture_session_id:
        raise RuntimeError("R6_COVERAGE_WINDOW_NOT_FOUND_OR_MISMATCH")
    if window.capture_attempt_id not in {None, capture_attempt_id}:
        raise RuntimeError("R6_COVERAGE_ATTEMPT_MISMATCH")
    if capture.evidence_durable_at is None:
        raise RuntimeError("R6_CAPTURE_EVIDENCE_NOT_DURABLE")
    by_channel = {str(t.channel): t for t in tracks}
    for channel in ("FXS", "PCAP", "PCM_RX", "PCM_TX"):
        if channel not in by_channel:
            raise RuntimeError(f"R6_COVERAGE_TRACK_MISSING:{channel}")
    return {
        "dialect": dialect,
        "capture": capture,
        "attempt": attempt,
        "window": window,
        "tracks": by_channel,
    }


def _recalculate_pcap_and_finalize(facts: dict[str, Any]) -> dict[str, Any]:
    capture = facts["capture"]
    window = facts["window"]
    pcap_track = facts["tracks"]["PCAP"]
    evidence, uncertain, reasons = PcapCoverageEvidenceBuilder(SessionLocal).build(
        capture_session_id=str(capture.id),
        required_start=window.required_start_ts,
        required_end=window.required_end_ts,
    )
    ledger = CoverageLedgerService(SessionLocal)
    result = ledger.calculate_track(
        coverage_window_id=str(window.id),
        channel="PCAP",
        requirement=str(pcap_track.requirement),
        evidence=evidence,
        applicable=True,
        uncertain_boundary=uncertain,
    )
    final = ledger.finalize_window(str(window.id))
    with SessionLocal() as db:
        tracks = list(db.scalars(select(CoverageTrack).where(
            CoverageTrack.coverage_window_id == str(window.id)
        )))
        current = db.get(CoverageWindow, str(window.id))
    track_snapshot = {
        str(t.channel): {
            "track_id": str(t.id),
            "requirement": str(t.requirement),
            "status": str(t.status),
            "required_ms": int(t.required_ms or 0),
            "covered_ms": int(t.covered_ms or 0),
            "gap_ms": int(t.gap_ms or 0),
            "unknown_ms": int(t.unknown_ms or 0),
        }
        for t in tracks
    }
    required = [row for row in track_snapshot.values()
                if row["requirement"] in {"REQUIRED", "CONDITIONAL_REQUIRED"}]
    if final != CoverageStatus.COMPLETE or current is None or current.status != CoverageStatus.COMPLETE.value:
        raise RuntimeError(
            "R6_COVERAGE_RECALC_NOT_COMPLETE:"
            + json.dumps({"pcap_reasons": reasons, "tracks": track_snapshot}, sort_keys=True)
        )
    if not required or not all(
        row["status"] == CoverageStatus.COMPLETE.value
        and row["covered_ms"] == row["required_ms"]
        and row["gap_ms"] == 0
        and row["unknown_ms"] == 0
        for row in required
    ):
        raise RuntimeError("R6_REQUIRED_COVERAGE_TRACKS_NOT_COMPLETE")
    facts["tracks"] = {str(t.channel): t for t in tracks}
    facts["window"] = current
    return {
        "pcap_status": result.status.value,
        "pcap_uncertain_boundary": uncertain,
        "pcap_uncertain_reasons": list(reasons),
        "window_status": current.status,
        "window_finalized_at": current.finalized_at.isoformat() if current.finalized_at else None,
        "tracks": track_snapshot,
    }


def _validated_pcap_segments(facts: dict[str, Any]) -> list[CaptureSegment]:
    capture = facts["capture"]
    window = facts["window"]
    with SessionLocal() as db:
        segments = list(db.scalars(select(CaptureSegment).where(
            CaptureSegment.capture_session_id == str(capture.id),
            CaptureSegment.state.in_(tuple(DURABLE_SEGMENT_STATES)),
            CaptureSegment.storage_key.is_not(None),
            CaptureSegment.server_size.is_not(None),
            CaptureSegment.sha256.is_not(None),
            CaptureSegment.pcap_valid.is_(True),
        ).order_by(CaptureSegment.segment_seq)))
        epochs = list(db.scalars(select(CaptureEpoch).where(
            CaptureEpoch.capture_session_id == str(capture.id)
        )))
    relevant = [
        row for row in segments
        if _overlaps(row.first_packet_ts or row.last_packet_ts,
                     row.last_packet_ts or row.first_packet_ts,
                     window.required_start_ts, window.required_end_ts)
    ]
    if not relevant:
        raise RuntimeError("R6_DURABLE_PCAP_SEGMENT_FOR_WINDOW_NOT_FOUND")
    store = LocalDurableSegmentStore(Path(settings.reproduction_object_root))
    for row in relevant:
        if not store.verify(
            storage_key=str(row.storage_key),
            size=int(row.server_size),
            sha256=str(row.sha256),
        ):
            raise RuntimeError(f"R6_PCAP_SERVER_COPY_VERIFY_FAILED:{row.id}")
    overlapping_epochs = [
        row for row in epochs
        if _overlaps(row.started_at, row.ended_at,
                     window.required_start_ts, window.required_end_ts)
    ]
    if not overlapping_epochs:
        raise RuntimeError("R6_OVERLAPPING_CAPTURE_EPOCH_NOT_FOUND")
    unknown_or_drop = [
        {"epoch_id": str(row.id), "kernel_drops": row.packets_dropped_kernel}
        for row in overlapping_epochs
        if row.packets_dropped_kernel is None or int(row.packets_dropped_kernel) != 0
    ]
    if unknown_or_drop:
        raise RuntimeError("R6_KERNEL_DROP_PROOF_FAILED:" + json.dumps(unknown_or_drop))
    return relevant


def _fxs_timeline(facts: dict[str, Any], golden: dict[str, Any]) -> tuple[list[CaptureEvent], list[dict[str, Any]]]:
    capture = facts["capture"]
    window = facts["window"]
    with SessionLocal() as db:
        rows = list(db.scalars(select(CaptureEvent).where(
            CaptureEvent.capture_session_id == str(capture.id),
            CaptureEvent.entity_type == "FXS_RAW",
            CaptureEvent.source_ts >= window.required_start_ts,
            CaptureEvent.source_ts <= window.required_end_ts,
        ).order_by(CaptureEvent.source_ts, CaptureEvent.recorded_at)))
    dtmf = "".join(
        str((row.payload or {}).get("digit") or "")
        for row in rows if row.event_type == "FXS_RAW_DTMF"
    )
    if dtmf != str(golden["observed_dtmf_digits"]):
        raise RuntimeError(f"R6_DB_DTMF_TIMELINE_MISMATCH:{dtmf}")
    event_types = [row.event_type for row in rows]
    if "FXS_RAW_OFFHOOK" not in event_types or "FXS_RAW_ONHOOK" not in event_types:
        raise RuntimeError("R6_DB_FXS_BOUNDARY_EVENTS_MISSING")
    timeline = [
        {
            "event_id": str(row.id),
            "event": str(row.event_type).removeprefix("FXS_RAW_"),
            "digit": (row.payload or {}).get("digit"),
            "line": (row.payload or {}).get("line"),
            "source_ts": _utc(row.source_ts).isoformat() if row.source_ts else None,
        }
        for row in rows
    ]
    return rows, timeline


def _materialize_assets(facts: dict[str, Any], golden: dict[str, Any],
                        segments: list[CaptureSegment], fxs_rows: list[CaptureEvent]) -> dict[str, str]:
    capture_session_id = str(facts["capture"].id)
    attempt_id = str(facts["attempt"].id)
    window = facts["window"]
    repo = EvidenceAssetRepository(SessionLocal)
    prefix = f"r6-abnormal-golden:{capture_session_id}:{attempt_id}"

    pcap_ids = []
    for row in segments:
        pcap_ids.append(repo.create(
            capture_session_id=capture_session_id,
            capture_attempt_id=attempt_id,
            call_ref=window.call_ref,
            asset_type="PCAP",
            title=f"R6 abnormal Golden durable PCAP segment {int(row.segment_seq)}",
            description="Immutable server-verified Capture V2 PCAP overlapping the first 8000 call coverage window.",
            storage_key=str(row.storage_key),
            source_refs=[f"capture-segment:{row.id}", f"capture-epoch:{row.capture_epoch_id}"],
            start_ts=row.first_packet_ts,
            end_ts=row.last_packet_ts,
            metadata={
                "product_evidence": True,
                "scenario": "APF1250_FIRST_8000_ABNORMAL_GOLDEN",
                "segment_id": str(row.id),
                "segment_seq": int(row.segment_seq),
                "server_size": int(row.server_size),
                "sha256": str(row.sha256),
                "packet_count": int(row.packet_count or 0),
                "pcap_valid": bool(row.pcap_valid),
            },
            idempotency_key=f"{prefix}:pcap:{row.id}",
        ))

    fxs_id = repo.create(
        capture_session_id=capture_session_id,
        capture_attempt_id=attempt_id,
        call_ref=window.call_ref,
        asset_type="FXS",
        title="R6 abnormal Golden AIM/FXS source-time event timeline",
        description="Real raw FXS timeline proving the panel target 8000 was observed by AIM/FXS as DTMF 000.",
        storage_key=None,
        source_refs=[f"capture-event:{row.id}" for row in fxs_rows],
        start_ts=fxs_rows[0].source_ts if fxs_rows else None,
        end_ts=fxs_rows[-1].source_ts if fxs_rows else None,
        metadata={
            "product_evidence": True,
            "scenario": "APF1250_FIRST_8000_ABNORMAL_GOLDEN",
            "expected_panel_target": "8000",
            "observed_dtmf_digits": "000",
            "finding": EXPECTED_FINDING,
            "event_count": len(fxs_rows),
        },
        idempotency_key=f"{prefix}:fxs",
    )

    extra: dict[str, str] = {}
    integrity = dict(golden.get("capture_integrity") or {})
    for channel, packet_key in (("PCM_RX", "pcm_rx_packets"), ("PCM_TX", "pcm_tx_packets")):
        track = facts["tracks"][channel]
        extra[channel] = repo.create(
            capture_session_id=capture_session_id,
            capture_attempt_id=attempt_id,
            call_ref=window.call_ref,
            asset_type=channel,
            title=f"R6 abnormal Golden {channel} coverage evidence",
            description=f"Persisted Coverage Ledger proof for {channel} during the abnormal first 8000 call.",
            storage_key=None,
            source_refs=[f"coverage-track:{track.id}"],
            start_ts=window.required_start_ts,
            end_ts=window.required_end_ts,
            metadata={
                "product_evidence": True,
                "coverage_status": str(track.status),
                "required_ms": int(track.required_ms or 0),
                "covered_ms": int(track.covered_ms or 0),
                "gap_ms": int(track.gap_ms or 0),
                "unknown_ms": int(track.unknown_ms or 0),
                "observed_packet_count": int(integrity.get(packet_key) or 0),
            },
            idempotency_key=f"{prefix}:{channel.lower()}",
        )
    return {
        "PCAP": pcap_ids,
        "FXS": fxs_id,
        **extra,
    }


def _build_product_report(facts: dict[str, Any], golden: dict[str, Any], assets: dict[str, Any]) -> dict[str, Any]:
    capture_session_id = str(facts["capture"].id)
    attempt_id = str(facts["attempt"].id)
    window = facts["window"]
    quality_reporter = CaptureV2FQualityReporter(SessionLocal)
    track_details = {
        channel: {
            "coverage_track_id": str(track.id),
            "status": str(track.status),
            "required_ms": int(track.required_ms or 0),
            "covered_ms": int(track.covered_ms or 0),
            "gap_ms": int(track.gap_ms or 0),
            "unknown_ms": int(track.unknown_ms or 0),
        }
        for channel, track in facts["tracks"].items()
        if channel in {"FXS", "PCAP", "PCM_RX", "PCM_TX"}
    }
    signals = [
        SignalEvidence(
            channel=channel,
            expected=True,
            captured=True,
            usable=True,
            details=track_details[channel],
        )
        for channel in ("FXS", "PCAP", "PCM_RX", "PCM_TX")
    ]
    quality_snapshot_id, quality = quality_reporter.evaluate_from_coverage(
        coverage_window_id=str(window.id),
        capture_session_id=capture_session_id,
        capture_attempt_id=attempt_id,
        call_ref=window.call_ref,
        signals=signals,
        required_channels_for_diagnosis=("FXS", "PCAP"),
        independent_support_count=2,
        contradictions=(),
        policy_version="capture-quality-v2.1-r6-product-materialization",
    )
    evidence_ids = tuple([*assets["PCAP"], assets["FXS"], assets["PCM_RX"], assets["PCM_TX"]])
    finding = dict(golden.get("finding") or {})
    report = quality_reporter.build_report_from_snapshot(
        capture_session_id=capture_session_id,
        quality_snapshot_id=quality_snapshot_id,
        findings=[FindingEvidenceRequest(
            finding_id="R6_FIRST_DIGIT_8_MISSING_BY_AIM_FXS",
            title="First expected digit 8 is missing by the AIM/FXS DTMF event layer",
            conclusion=EXPECTED_FINDING,
            confidence="HIGH",
            required_asset_types=("PCAP", "FXS"),
            evidence_asset_ids=evidence_ids,
            why=tuple(str(item) for item in (finding.get("why") or [])),
        )],
    )
    row = report["findings"][0]
    if not row.get("supported"):
        raise RuntimeError("R6_PRODUCT_REPORT_FINDING_UNSUPPORTED:" + json.dumps(row, default=str))
    if row.get("conclusion") != EXPECTED_FINDING or row.get("confidence") != "HIGH":
        raise RuntimeError("R6_PRODUCT_REPORT_CONCLUSION_OR_CONFIDENCE_MISMATCH")
    if quality.get("capture_completeness") != "COMPLETE" or quality.get("diagnostic_confidence") != "HIGH":
        raise RuntimeError("R6_PRODUCT_REPORT_QUALITY_NOT_COMPLETE_HIGH:" + json.dumps(quality))
    return {
        "quality_snapshot_id": quality_snapshot_id,
        "quality": quality,
        "manifest": report,
    }


def materialize(*, repo_root: Path, golden_path: Path) -> tuple[int, dict[str, Any]]:
    repo_root = repo_root.resolve()
    golden_path = golden_path.resolve()
    allowed = (repo_root / "validation/capture_v2/R6_APF1250_FIRST_8000_ABNORMAL_GOLDEN_RC33.json").resolve()
    if golden_path != allowed:
        return 1, {"verdict": "FAIL", "reason": "R6_GOLDEN_PATH_NOT_ALLOWED"}
    try:
        golden = _load_golden(golden_path)
        facts = _capture_facts(golden)
        coverage = _recalculate_pcap_and_finalize(facts)
        segments = _validated_pcap_segments(facts)
        fxs_rows, timeline = _fxs_timeline(facts, golden)
        assets = _materialize_assets(facts, golden, segments, fxs_rows)
        report = _build_product_report(facts, golden, assets)
        checks = {
            "real_postgresql": facts["dialect"] == "postgresql",
            "capture_evidence_durable": facts["capture"].evidence_durable_at is not None,
            "coverage_recalculated_complete": coverage["window_status"] == "COMPLETE",
            "pcap_uncertain_boundary_cleared": coverage["pcap_uncertain_boundary"] is False,
            "durable_pcap_server_copy_verified": bool(segments),
            "kernel_capture_drops_zero": int((golden.get("capture_integrity") or {}).get("kernel_capture_drops") or 0) == 0,
            "fxs_db_digits_exact_000": "".join(
                str(row.get("digit") or "") for row in timeline if row["event"] == "DTMF"
            ) == "000",
            "evidence_assets_materialized": bool(assets.get("PCAP")) and bool(assets.get("FXS")),
            "quality_complete_high": report["quality"].get("capture_completeness") == "COMPLETE"
                and report["quality"].get("diagnostic_confidence") == "HIGH",
            "finding_supported": report["manifest"]["findings"][0].get("supported") is True,
            "finding_conclusion_exact": report["manifest"]["findings"][0].get("conclusion") == EXPECTED_FINDING,
        }
        if not all(checks.values()):
            return 1, {"verdict": "FAIL", "reason": "R6_PRODUCT_REPORT_MATERIALIZATION_CHECK_FAILED", "checks": checks}
        return 0, {
            "verdict": "PASS",
            "reason": "R6_ABNORMAL_GOLDEN_PRODUCT_REPORT_MATERIALIZED",
            "golden": str(golden_path.relative_to(repo_root)),
            "capture_session_id": str(facts["capture"].id),
            "capture_attempt_id": str(facts["attempt"].id),
            "coverage_window_id": str(facts["window"].id),
            "coverage": coverage,
            "fxs_timeline": timeline,
            "evidence_assets": assets,
            "report": report,
            "checks": checks,
            "production_mutation": False,
            "dut_mutation": False,
        }
    except Exception as exc:
        return 1, {
            "verdict": "FAIL",
            "reason": "R6_PRODUCT_REPORT_MATERIALIZATION_EXCEPTION",
            "error": f"{type(exc).__name__}:{exc}",
            "production_mutation": False,
            "dut_mutation": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize R6 abnormal Golden product EvidenceAssets/report")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--golden-path", type=Path, required=True)
    args = parser.parse_args(argv)
    rc, payload = materialize(repo_root=args.repo_root, golden_path=args.golden_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

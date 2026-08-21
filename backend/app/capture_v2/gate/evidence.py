from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from app.capture_v2.db_models import (
    AttemptDataPlaneVerification,
    CaptureAttempt,
    CaptureEpoch,
    CaptureEvent,
    CaptureGap,
    CaptureLease,
    CaptureSegment,
    CaptureSession,
    CoverageInterval,
    CoverageTrack,
    CoverageWindow,
    EvidenceAsset,
    QualitySnapshot,
    ReadinessSnapshot,
    SignalAvailability,
)
from app.capture_v2.gate.models import GateRunPaths
from app.capture_v2.storage.local import sha256_file
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _model_dict(row) -> dict[str, Any]:
    return {
        column.name: _jsonable(getattr(row, column.name))
        for column in row.__table__.columns
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _git_revision(repo_root: Path | None = None) -> dict[str, Any]:
    cwd = str(repo_root) if repo_root else None
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=cwd, text=True).strip())
        return {"sha": sha, "branch": branch, "dirty": dirty}
    except Exception:
        return {"sha": None, "branch": None, "dirty": None}


_TABLES = (
    ("capture_sessions", CaptureSession),
    ("capture_leases", CaptureLease),
    ("capture_epochs", CaptureEpoch),
    ("capture_events", CaptureEvent),
    ("capture_gaps", CaptureGap),
    ("capture_segments", CaptureSegment),
    ("readiness_snapshots", ReadinessSnapshot),
    ("capture_attempts", CaptureAttempt),
    ("attempt_data_plane_verifications", AttemptDataPlaneVerification),
    ("coverage_windows", CoverageWindow),
    ("coverage_tracks", CoverageTrack),
    ("coverage_intervals", CoverageInterval),
    ("quality_snapshots", QualitySnapshot),
    ("signal_availability", SignalAvailability),
    ("evidence_assets", EvidenceAsset),
)


class GateEvidenceCollector:
    """Read-only, deterministic evidence collection for real Gate execution."""

    def __init__(self, *, session_factory, adapter=None, object_root: Path | None = None, repo_root: Path | None = None):
        self.session_factory = session_factory
        self.adapter = adapter
        self.object_root = Path(object_root) if object_root else None
        self.repo_root = Path(repo_root) if repo_root else None

    def _rows_for(self, model, *, capture_session_id: str, device_id: str | None) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            stmt = select(model)
            columns = model.__table__.columns.keys()
            if model is CaptureSession:
                stmt = stmt.where(CaptureSession.id == capture_session_id)
            elif model is CaptureLease:
                if device_id is None:
                    return []
                stmt = stmt.where(CaptureLease.device_id == device_id)
            elif "capture_session_id" in columns:
                stmt = stmt.where(getattr(model, "capture_session_id") == capture_session_id)
            elif model is AttemptDataPlaneVerification:
                stmt = stmt.join(
                    CaptureAttempt, AttemptDataPlaneVerification.capture_attempt_id == CaptureAttempt.id
                ).where(CaptureAttempt.capture_session_id == capture_session_id)
            elif model is CoverageTrack:
                stmt = stmt.join(
                    CoverageWindow, CoverageTrack.coverage_window_id == CoverageWindow.id
                ).where(CoverageWindow.capture_session_id == capture_session_id)
            elif model is CoverageInterval:
                stmt = stmt.join(
                    CoverageTrack, CoverageInterval.coverage_track_id == CoverageTrack.id
                ).join(
                    CoverageWindow, CoverageTrack.coverage_window_id == CoverageWindow.id
                ).where(CoverageWindow.capture_session_id == capture_session_id)
            elif model is SignalAvailability:
                stmt = stmt.join(
                    QualitySnapshot, SignalAvailability.quality_snapshot_id == QualitySnapshot.id
                ).where(QualitySnapshot.capture_session_id == capture_session_id)
            elif "device_id" in columns and device_id is not None:
                stmt = stmt.where(getattr(model, "device_id") == device_id)
            return [_model_dict(row) for row in db.scalars(stmt).all()]

    def collect_db(self, *, paths: GateRunPaths, capture_session_id: str, device_id: str | None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, model in _TABLES:
            rows = self._rows_for(model, capture_session_id=capture_session_id, device_id=device_id)
            _write_json(paths.db_dir / f"{name}.json", rows)
            counts[name] = len(rows)
        return counts

    async def collect_dut(self, *, paths: GateRunPaths) -> dict[str, Any]:
        if self.adapter is None:
            return {"available": False}
        reader = ReadOnlyDeviceTransport(self.adapter)
        result: dict[str, Any] = {"available": True}
        queries = {
            "uname.txt": "uname -a",
            "df_tmp.txt": "df -k /tmp 2>/dev/null || df -k",
            "control_tree.txt": (
                "if [ -d /tmp/aivoip_capture ]; then "
                "find /tmp/aivoip_capture -maxdepth 4 -type f -print 2>/dev/null | sort; fi"
            ),
            "control_values.txt": (
                "for f in /tmp/aivoip_capture/control/*; do "
                "[ -f \"$f\" ] || continue; printf '--- %s ---\\n' \"$f\"; cat \"$f\"; printf '\\n'; done; true"
            ),
            "voice_serv_info.txt": "dev_config get -m voipServInfo 2>/dev/null || true",
            "voice_vlan.txt": "dev_config get -m voice_vlan 2>/dev/null || true",
            "links.txt": "ip -o link show 2>/dev/null || true",
        }
        try:
            result["boot_id"] = await reader.boot_id()
            (paths.dut_dir / "boot_id.txt").write_text(result["boot_id"] + "\n", encoding="utf-8")
        except Exception as exc:
            result["boot_id_error"] = type(exc).__name__
        try:
            processes = await reader.list_tcpdump_processes()
            result["tcpdump_processes"] = [_jsonable(p) for p in processes]
            _write_json(paths.dut_dir / "tcpdump_processes.json", result["tcpdump_processes"])
        except Exception as exc:
            result["tcpdump_error"] = type(exc).__name__
        try:
            result["epoch_dirs"] = await reader.list_epoch_dirs()
            _write_json(paths.dut_dir / "epoch_dirs.json", result["epoch_dirs"])
        except Exception as exc:
            result["epoch_dirs_error"] = type(exc).__name__
        try:
            result["legacy_ring_dirs"] = await reader.list_legacy_ring_dirs()
            _write_json(paths.dut_dir / "legacy_ring_dirs.json", result["legacy_ring_dirs"])
        except Exception as exc:
            result["legacy_ring_error"] = type(exc).__name__
        for filename, command in queries.items():
            try:
                text = await reader.run(command)
                (paths.dut_dir / filename).write_text(text, encoding="utf-8")
            except Exception as exc:
                (paths.dut_dir / f"{filename}.error").write_text(type(exc).__name__ + "\n", encoding="utf-8")
        return result

    def collect_server_store(self, *, paths: GateRunPaths, capture_session_id: str) -> dict[str, Any]:
        inventory: list[dict[str, Any]] = []
        with self.session_factory() as db:
            segments = list(db.scalars(select(CaptureSegment).where(
                CaptureSegment.capture_session_id == capture_session_id
            )))
        for row in segments:
            item = {
                "segment_id": row.id,
                "segment_seq": int(row.segment_seq),
                "state": row.state,
                "storage_key": row.storage_key,
                "db_sha256": row.sha256,
                "db_server_size": row.server_size,
                "exists": None,
                "actual_size": None,
                "actual_sha256": None,
            }
            if self.object_root is not None and row.storage_key:
                path = self.object_root / row.storage_key
                item["path"] = str(path)
                item["exists"] = path.is_file()
                if path.is_file():
                    item["actual_size"] = path.stat().st_size
                    item["actual_sha256"] = sha256_file(path)
            inventory.append(item)
        _write_json(paths.server_dir / "store_inventory.json", inventory)
        return {"objects": inventory}

    async def collect(
        self,
        *,
        paths: GateRunPaths,
        gate_id: str,
        capture_session_id: str,
        device_id: str | None,
        facts: dict[str, Any] | None = None,
    ) -> Path:
        db_counts = self.collect_db(paths=paths, capture_session_id=capture_session_id, device_id=device_id)
        dut = await self.collect_dut(paths=paths)
        store = self.collect_server_store(paths=paths, capture_session_id=capture_session_id)
        manifest = {
            "gate_id": gate_id,
            "capture_session_id": capture_session_id,
            "device_id": device_id,
            "git": _git_revision(self.repo_root),
            "db_counts": db_counts,
            "dut_summary": dut,
            "server_store_count": len(store["objects"]),
            "facts": facts or {},
            "pid": os.getpid(),
        }
        _write_json(paths.case_dir / "manifest.json", manifest)
        return paths.case_dir

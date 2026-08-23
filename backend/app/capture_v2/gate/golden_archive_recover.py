from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import tarfile
from pathlib import Path

from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec


_DATE_RE = re.compile(r"^20\d{6}$")
_MODEL_ARCHIVE_LABEL = {
    "APF1250": "APF1250",
    "APF3260-M": "APF3260",
}
_LOCAL_ROOT = Path("/tmp/capture-v2-golden-recovery")
_REMOTE_ROOTS = ("/www", "/tmp")


def archive_name_for(model: str, archive_date: str) -> str:
    label = _MODEL_ARCHIVE_LABEL.get(str(model))
    if label is None:
        raise ValueError("GOLDEN_ARCHIVE_MODEL_NOT_ALLOWED")
    if not _DATE_RE.fullmatch(str(archive_date)):
        raise ValueError("GOLDEN_ARCHIVE_DATE_INVALID")
    return f"v21_golden_{label}_{archive_date}.tar.gz"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_archive(path: Path) -> dict:
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = tf.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("GOLDEN_ARCHIVE_INVALID_TAR") from exc

    names = [m.name for m in members]
    unsafe = [n for n in names if n.startswith("/") or ".." in Path(n).parts]
    if unsafe:
        raise ValueError("GOLDEN_ARCHIVE_UNSAFE_MEMBER")
    pcap_names = [n for n in names if n.lower().endswith((".pcap", ".pcapng"))]
    regular_bytes = sum(int(m.size or 0) for m in members if m.isfile())
    return {
        "member_count": len(members),
        "pcap_count": len(pcap_names),
        "regular_file_bytes": regular_bytes,
        "pcap_names": pcap_names[:200],
        "members": names[:300],
        "members_truncated": len(names) > 300,
    }


async def recover(args) -> int:
    archive_name = archive_name_for(args.model, args.archive_date)
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=int(args.port),
        username=args.username,
        platform_id=args.platform_id,
    )
    local_dir = _LOCAL_ROOT / spec.device_id / args.archive_date
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / archive_name

    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    await adapter.connect()
    found_root = None
    attempts: list[dict[str, str]] = []
    try:
        for root in _REMOTE_ROOTS:
            try:
                local_path.unlink(missing_ok=True)
                await adapter.scp_get(f"{root}/{archive_name}", str(local_path), timeout=90)
                if local_path.is_file() and local_path.stat().st_size > 0:
                    found_root = root
                    break
                attempts.append({"root": root, "result": "EMPTY_OR_MISSING"})
            except Exception as exc:
                attempts.append({"root": root, "result": type(exc).__name__})
                local_path.unlink(missing_ok=True)
    finally:
        await adapter.disconnect()

    if found_root is None:
        print(json.dumps({
            "verdict": "INCONCLUSIVE",
            "reason": "GOLDEN_ARCHIVE_NOT_FOUND",
            "archive_name": archive_name,
            "attempts": attempts,
        }, ensure_ascii=False, indent=2))
        return 2

    try:
        inventory = inspect_archive(local_path)
    except ValueError as exc:
        print(json.dumps({
            "verdict": "FAIL",
            "reason": str(exc),
            "archive_name": archive_name,
            "source_root": found_root,
            "local_path": str(local_path),
            "size_bytes": local_path.stat().st_size,
            "sha256": sha256_file(local_path),
        }, ensure_ascii=False, indent=2))
        return 2

    archive_sha = sha256_file(local_path)
    payload = {
        "verdict": "PASS",
        "archive_name": archive_name,
        "source_root": found_root,
        "local_path": str(local_path),
        "size_bytes": local_path.stat().st_size,
        "sha256": archive_sha,
        "inventory": inventory,
        "attempts": attempts,
    }

    # The recovery Gate stays about archive retrieval. Offline analysis is
    # intentionally nested evidence: its result can strengthen R5/R6 evidence,
    # but it never upgrades this recovery PASS into an R5/R6 release PASS.
    try:
        from app.capture_v2.gate.golden_archive_analyze import analyze_archive
        payload["analysis"] = analyze_archive(
            device_id=spec.device_id,
            model=spec.model,
            archive_date=args.archive_date,
        )
    except Exception as exc:
        payload["analysis"] = {
            "verdict": "INCONCLUSIVE",
            "reason": f"GOLDEN_ARCHIVE_ANALYSIS_ERROR:{type(exc).__name__}:{exc}",
            "release_gate_effect": "EVIDENCE_ONLY_NOT_R5_PASS",
        }

    try:
        from app.capture_v2.gate.golden_archive_fallback import analyze_archive_fallback
        payload["fallback_analysis"] = analyze_archive_fallback(
            device_id=spec.device_id,
            model=spec.model,
            archive_date=args.archive_date,
        )
    except Exception as exc:
        payload["fallback_analysis"] = {
            "verdict": "INCONCLUSIVE",
            "reason": f"GOLDEN_ARCHIVE_FALLBACK_ERROR:{type(exc).__name__}:{exc}",
            "release_gate_effect": "EVIDENCE_ONLY_NOT_R5_PASS",
            "parser": "PURE_PYTHON_CLASSIC_PCAP",
        }

    # Non-physical R5 closure aid: real PostgreSQL CoverageLedger runtime test.
    try:
        from app.capture_v2.gate.coverage_db_validation import validate_real_postgres_coverage_ledger
        payload["coverage_db_validation"] = validate_real_postgres_coverage_ledger(
            device_id=spec.device_id,
            marker=f"{spec.model}:{args.archive_date}:{archive_sha[:16]}",
        )
    except Exception as exc:
        payload["coverage_db_validation"] = {
            "verdict": "INCONCLUSIVE",
            "reason": f"REAL_POSTGRES_COVERAGE_LEDGER_SELF_TEST_ERROR:{type(exc).__name__}:{exc}",
            "release_gate_effect": "VALIDATES_LEDGER_RUNTIME_ONLY_NOT_ONLINE_R5_PASS",
        }

    # Non-physical R6 closure aid: real PostgreSQL evidence-first asset/report
    # semantics. Missing required evidence must erase the requested root-cause
    # conclusion, while complete selected evidence may retain a bounded finding.
    try:
        from app.capture_v2.gate.report_db_validation import validate_real_postgres_evidence_first_report
        payload["report_db_validation"] = validate_real_postgres_evidence_first_report(
            device_id=spec.device_id,
            marker=f"{spec.model}:{args.archive_date}:{archive_sha[:16]}",
        )
    except Exception as exc:
        payload["report_db_validation"] = {
            "verdict": "INCONCLUSIVE",
            "reason": f"REAL_POSTGRES_EVIDENCE_FIRST_SELF_TEST_ERROR:{type(exc).__name__}:{exc}",
            "release_gate_effect": "VALIDATES_EVIDENCE_FIRST_DB_RUNTIME_ONLY_NOT_ABNORMAL_E2E_PASS",
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Recover a fixed historical V2.1 Golden archive from a DUT")
    p.add_argument("--device-id", required=True)
    p.add_argument("--model", choices=sorted(_MODEL_ARCHIVE_LABEL), required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--username", default="root")
    p.add_argument("--platform-id", choices=["mt7621", "mt7981"], required=True)
    p.add_argument("--password-env", required=True)
    p.add_argument("--archive-date", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(recover(args))
    except ValueError as exc:
        print(json.dumps({"verdict": "FAIL", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

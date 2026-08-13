#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.release_readiness import runtime_release_readiness  # noqa: E402
if str(ROOT / 'tools') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tools'))
from release_evidence import load_source_bound_evidence  # noqa: E402


@dataclass
class Gate:
    key: str
    status: str
    blocking: bool
    category: str
    detail: str
    command: str | None = None


def run_gate(key: str, command: list[str], *, category: str = "STATIC", timeout_seconds: int = 90) -> Gate:
    print(f"[release-readiness] running {key}...", file=sys.stderr, flush=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    try:
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as fh:
            cp = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout_seconds)
            fh.seek(0); detail = fh.read()[-4000:].strip()
        return Gate(key, "PASS" if cp.returncode == 0 else "FAIL", True, category, detail, " ".join(command))
    except subprocess.TimeoutExpired:
        return Gate(key, "FAIL", True, category, f"gate timed out after {timeout_seconds}s", " ".join(command))


def runtime_probe(key: str, condition: bool | None, *, detail_pass: str, detail_fail: str, category: str = "RUNTIME") -> Gate:
    if condition is True:
        return Gate(key, "PASS", True, category, detail_pass)
    if condition is False:
        return Gate(key, "UNVERIFIED", True, category, detail_fail)
    return Gate(key, "UNVERIFIED", True, category, detail_fail)


def write_markdown(path: Path, payload: dict) -> None:
    lines = [
        "# VOIP AI V1.0 Release Readiness",
        "",
        f"- Overall: **{payload['status']}**",
        f"- Static gates: **{payload['static_status']}**",
        f"- Production readiness: **{payload['production_status']}**",
        f"- PASS: {payload['counts'].get('PASS', 0)} / BLOCKED: {payload['counts'].get('BLOCKED', 0)} / UNVERIFIED: {payload['counts'].get('UNVERIFIED', 0)} / FAIL: {payload['counts'].get('FAIL', 0)}",
        "",
        "| Gate | Status | Category | Blocking | Detail |",
        "|---|---|---|---:|---|",
    ]
    for row in payload["gates"]:
        detail = str(row["detail"]).replace("\n", " ").replace("|", "\\|")
        if len(detail) > 240:
            detail = detail[:237] + "..."
        lines.append(f"| {row['key']} | {row['status']} | {row['category']} | {'yes' if row['blocking'] else 'no'} | {detail} |")
    lines += ["", "## Blocking items", ""]
    blockers = [x for x in payload["gates"] if x["blocking"] and x["status"] != "PASS"]
    if not blockers:
        lines.append("None.")
    else:
        for row in blockers:
            lines.append(f"- **{row['key']}** — {row['status']}: {row['detail']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="return non-zero unless production readiness is PASS")
    ap.add_argument("--refresh-static", action="store_true", help="rerun all Phase F2 static gates before readiness evaluation")
    ap.add_argument("--out", type=Path, default=ROOT / "validation" / "v1_release_readiness.json")
    ap.add_argument("--markdown", type=Path, default=ROOT / "validation" / "v1_release_readiness.md")
    ap.add_argument("--field-evidence-dir", type=Path)
    args = ap.parse_args()

    gates: list[Gate] = []
    static_artifact = ROOT / "validation" / "phase_f3_static_gate.json"
    source_manifest = ROOT / "release" / "source_manifest.json"
    refresh_failed = False
    if args.refresh_static:
        refresh = run_gate("PHASE_F3_STATIC_REFRESH", ["sh", "tools/phase_f3_static_gate.sh"], timeout_seconds=600)
        if refresh.status != "PASS":
            gates.append(refresh)
            refresh_failed = True
    static_payload = None
    if not refresh_failed and static_artifact.exists() and source_manifest.exists():
        try:
            static_payload=json.loads(static_artifact.read_text(encoding="utf-8"))
            manifest_payload=json.loads(source_manifest.read_text(encoding="utf-8"))
            if static_payload.get("source_manifest_aggregate_sha256") != manifest_payload.get("aggregate_sha256"):
                static_payload=None
        except Exception:
            static_payload=None
    if static_payload is None:
        gates.append(Gate("PHASE_F3_STATIC_GATE", "UNVERIFIED", True, "STATIC", "No current static-gate artifact matches the exact source manifest; run tools/phase_f3_static_gate.sh."))
        static_status="UNVERIFIED"
    else:
        for row in static_payload.get("gates", []):
            if isinstance(row, str):
                gates.append(Gate(row, "PASS", True, "STATIC", "Validated by the exact-source Phase F2 static-gate artifact."))
            else:
                gates.append(Gate(row["key"], row["status"], True, "STATIC", row.get("detail", "")))
        static_status = "PASS" if static_payload.get("status") == "PASS" else "FAIL"

    # Runtime-visible production blockers.  These are expected to remain BLOCKED until the
    # reserved EC-02 and live integrations are deliberately completed.
    runtime = runtime_release_readiness(profile_root=ROOT / "profiles")
    for item in runtime["items"]:
        gates.append(Gate(
            key=item["key"], status=item["status"], blocking=item["blocking"],
            category=item["category"], detail=item["detail"]
        ))

    # Runtime artifacts are acceptable when they are exact-source evidence, even if this
    # coordinator host does not itself have Docker/npm.  Stale artifacts are UNVERIFIED.
    fullstack_path = ROOT / "validation" / "fullstack_result.json"
    fullstack, fullstack_reason = load_source_bound_evidence(fullstack_path, root=ROOT)
    fullstack_ok = bool(fullstack and fullstack.get("passed") is True)
    gates.append(Gate(
        "DOCKER_FULLSTACK_RUNTIME", "PASS" if fullstack_ok else "UNVERIFIED", True, "RUNTIME",
        "Exact-source Docker full-stack E2E passed." if fullstack_ok else f"Docker full-stack runtime is not verified for this source: {fullstack_reason}",
    ))
    migration_ok = bool(fullstack_ok and fullstack.get("migration_runtime_verified") is True)
    gates.append(Gate(
        "POSTGRES_MIGRATION_RUNTIME", "PASS" if migration_ok else "UNVERIFIED", True, "RUNTIME",
        "Exact-source full-stack evidence verified alembic_version equals the expected migration head." if migration_ok else "Real PostgreSQL migration-to-head has not been verified for the exact current source.",
    ))

    production_runtime_path = ROOT / "validation" / "production_runtime_result.json"
    production_runtime, production_runtime_reason = load_source_bound_evidence(production_runtime_path, root=ROOT)
    production_runtime_ok = bool(production_runtime and production_runtime.get("passed") is True)
    gates.append(Gate(
        "PRODUCTION_DEPLOYMENT_RUNTIME", "PASS" if production_runtime_ok else "UNVERIFIED", True, "RUNTIME",
        "Exact-source production deployment runtime verification passed." if production_runtime_ok else f"Production deployment runtime is not verified for this source: {production_runtime_reason}",
    ))

    lockfile = ROOT / "frontend" / "package-lock.json"
    gates.append(Gate(
        "FRONTEND_LOCKFILE", "PASS" if lockfile.exists() else "BLOCKED", True, "BUILD",
        "frontend/package-lock.json is source-controlled for reproducible npm ci builds." if lockfile.exists() else "frontend/package-lock.json is missing; a production frontend build is not reproducible and must not be promoted.",
    ))
    frontend_path = ROOT / "validation" / "frontend_build_runtime.json"
    frontend, frontend_reason = load_source_bound_evidence(frontend_path, root=ROOT)
    frontend_ok = bool(frontend and frontend.get("passed") is True and frontend.get("index_sha256"))
    gates.append(Gate(
        "FRONTEND_PRODUCTION_BUILD", "PASS" if frontend_ok else "UNVERIFIED", True, "BUILD",
        "Exact-source npm ci + production build evidence is present." if frontend_ok else f"Frontend production build is not verified for this source: {frontend_reason}",
    ))

    if args.field_evidence_dir:
        field_run = run_gate(
            "FIELD_GOLDEN_EXECUTION",
            [sys.executable, "tools/field_golden_batch.py", "--evidence-dir", str(args.field_evidence_dir), "--out", str(ROOT / "validation" / "phase_f3_field_golden.json"), "--require-all"],
            category="FIELD", timeout_seconds=120,
        )
        if field_run.status != "PASS":
            gates.append(Gate("FIELD_GOLDEN", "UNVERIFIED", True, "FIELD", f"Field Golden execution failed: {field_run.detail}"))
        else:
            prior = ROOT / "validation" / "phase_f3_field_golden.json"
            data, reason = load_source_bound_evidence(prior, root=ROOT)
            ok = bool(data and data.get("passed") is True)
            gates.append(Gate("FIELD_GOLDEN", "PASS" if ok else "UNVERIFIED", True, "FIELD", "Exact-source Field Golden passed." if ok else f"Field Golden is not valid for this source: {reason}"))
    else:
        prior = ROOT / "validation" / "phase_f3_field_golden.json"
        data, reason = load_source_bound_evidence(prior, root=ROOT)
        ok = bool(data and data.get("passed") is True)
        gates.append(Gate("FIELD_GOLDEN", "PASS" if ok else "UNVERIFIED", True, "FIELD", "Exact-source Field Golden passed." if ok else f"Field Golden is not verified for this source: {reason}"))

    counts: dict[str, int] = {}
    for x in gates:
        counts[x.status] = counts.get(x.status, 0) + 1
    blockers = [x for x in gates if x.blocking and x.status != "PASS"]
    production_status = "PASS" if not blockers else "BLOCKED"
    overall = "PASS" if static_status == "PASS" and production_status == "PASS" else ("STATIC_PASS_PRODUCTION_BLOCKED" if static_status == "PASS" else "FAIL")
    payload = {
        "schema_version": 1,
        "release_id": "VOIP_AI_V1.0",
        "status": overall,
        "static_status": static_status,
        "production_status": production_status,
        "counts": counts,
        "gates": [asdict(x) for x in gates],
        "blocking_keys": [x.key for x in blockers],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(args.markdown, payload)
    print(json.dumps({
        "status": overall,
        "static_status": static_status,
        "production_status": production_status,
        "counts": counts,
        "blocking_keys": payload["blocking_keys"],
        "result": str(args.out),
    }, ensure_ascii=False, indent=2))
    if static_status != "PASS":
        return 1
    if args.strict and production_status != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

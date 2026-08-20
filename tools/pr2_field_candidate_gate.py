#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.analyzers.media.engine import MediaIntelligenceEngine  # noqa: E402
from app.analyzers.pcm.profile import load_pcm_profile  # noqa: E402
from app.reports.finding_composer import compose_findings  # noqa: E402


DEFAULT_CONTRACT = ROOT / "golden_cases" / "pr2_field_20260814_candidate_decision.json"
DEFAULT_PCM_PROFILE = ROOT / "profiles" / "pcm" / "ruijie_aim_diag_v1.yaml"
DEFAULT_OUT = ROOT / "validation" / "pr2_field_candidate_gate.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(checks: list[dict], key: str, ok: bool, detail: Any) -> None:
    checks.append({"key": key, "status": "PASS" if ok else "FAIL", "detail": detail})


def _pcm_sequences(pcm: dict) -> list[str]:
    out: list[str] = []
    for stream in pcm.get("streams", []) or []:
        for session in stream.get("sessions", []) or []:
            for seq in session.get("dtmf_sequences", []) or []:
                digits = str(seq.get("digits") or "")
                if digits:
                    out.append(digits)
    return out


def _closest_raw_click(pcm: dict, *, tap_name: str, target_time: float) -> dict | None:
    rows = []
    for stream in pcm.get("streams", []) or []:
        if (stream.get("tap") or {}).get("name") != tap_name:
            continue
        for session in stream.get("sessions", []) or []:
            base = float(session.get("start_time") or 0.0)
            for event in session.get("click_pop_events", []) or []:
                absolute_time = base + float(event.get("time_seconds") or 0.0)
                rows.append({
                    "absolute_time": absolute_time,
                    "delta_ms": abs(absolute_time - target_time) * 1000.0,
                    "event": event,
                    "decision": event.get("candidate_decision") or {},
                })
    return min(rows, key=lambda x: x["delta_ms"]) if rows else None


def _best_pcm_rtp_correlation(media: dict, *, tap_name: str) -> dict | None:
    rows = []
    for event in media.get("correlations", []) or []:
        if event.get("type") != "PCM_RTP_CORRELATION":
            continue
        details = event.get("details") or {}
        if details.get("pcm_tap") != tap_name:
            continue
        corr = details.get("correlation") or {}
        rows.append({
            "rtp_stream_id": details.get("rtp_stream_id"),
            "pcm_session_index": details.get("pcm_session_index"),
            "absolute_correlation": abs(float(corr.get("absolute_correlation") or 0.0)),
            "quality": corr.get("quality"),
            "lag_ms": float(corr.get("lag_ms") or 0.0),
        })
    return max(rows, key=lambda x: x["absolute_correlation"]) if rows else None


def _silence_decisions(media: dict, *, tap_name: str) -> list[dict]:
    out = []
    for event in media.get("active_media_audio_events", []) or []:
        if event.get("type") != "UNEXPECTED_SILENCE":
            continue
        scope = event.get("scope") or {}
        if scope.get("pcm_tap") != tap_name:
            continue
        decision = (event.get("details") or {}).get("candidate_decision") or {}
        out.append({
            "time": event.get("time"),
            "start_time": event.get("start_time"),
            "end_time": event.get("end_time"),
            "status": decision.get("status"),
            "reason_code": decision.get("reason_code"),
            "candidate_id": decision.get("candidate_id"),
            "positive_evidence": decision.get("positive_evidence") or {},
        })
    return out


def evaluate(pcap: Path, contract: dict, pcm_profile_path: Path, work_dir: Path) -> dict:
    checks: list[dict] = []
    expected = contract.get("expected") or {}
    source = contract.get("source") or {}

    actual_sha = _sha256(pcap)
    actual_size = pcap.stat().st_size
    _check(checks, "SOURCE_SHA256", actual_sha == source.get("sha256"), {
        "expected": source.get("sha256"), "actual": actual_sha,
    })
    _check(checks, "SOURCE_SIZE", actual_size == int(source.get("size_bytes") or -1), {
        "expected": source.get("size_bytes"), "actual": actual_size,
    })
    if any(x["status"] == "FAIL" for x in checks):
        return {
            "schema_version": "pr2-field-candidate-gate-v1",
            "contract_id": contract.get("id"),
            "status": "FAIL",
            "checks": checks,
            "note": "Source identity failed; analyzer execution skipped.",
        }

    pcm_profile = load_pcm_profile(pcm_profile_path)
    engine = MediaIntelligenceEngine(pcm_profile)
    media = engine.analyze_pcap(pcap, work_dir)
    pcm = media.get("pcm") or {}
    packet = media.get("packet") or {}

    expected_profiles = contract.get("profiles") or {}
    analyzer_profile = media.get("analyzer_profile") or {}
    _check(checks, "ANALYZER_PROFILE", (
        analyzer_profile.get("profile_id") == expected_profiles.get("analyzer_profile_id")
        and analyzer_profile.get("profile_version") == expected_profiles.get("analyzer_profile_version")
    ), {"expected": expected_profiles, "actual": analyzer_profile})

    calls = packet.get("calls", []) or []
    call_ids = [str(c.get("call_id") or "") for c in calls]
    _check(checks, "SIP_CALL_ID", str(expected.get("sip_call_id")) in call_ids, {
        "expected": expected.get("sip_call_id"), "actual": call_ids,
    })

    sequences = _pcm_sequences(pcm)
    _check(checks, "DTMF_SEQUENCE", str(expected.get("dtmf_sequence")) in sequences, {
        "expected": expected.get("dtmf_sequence"), "actual": sequences,
    })

    click_exp = expected.get("raw_pcm_click_negative_control") or {}
    click = _closest_raw_click(
        pcm,
        tap_name=str(click_exp.get("pcm_tap") or "pcm_rx"),
        target_time=float(click_exp.get("absolute_time") or 0.0),
    )
    click_ok = bool(click) and click["delta_ms"] <= float(click_exp.get("time_tolerance_ms") or 0.0)
    if click_ok:
        click_ok = (
            click["decision"].get("status") == click_exp.get("status")
            and click["decision"].get("reason_code") == click_exp.get("reason_code")
        )
    _check(checks, "RAW_CLICK_NEGATIVE_CONTROL", click_ok, {
        "expected": click_exp,
        "actual": click,
    })

    silence_exp = expected.get("pcm_tx_silence") or {}
    silence = _silence_decisions(media, tap_name="pcm_tx")
    promoted = sum(1 for x in silence if x.get("status") == "PROMOTED")
    suppressed = sum(1 for x in silence if x.get("status") == "SUPPRESSED")
    reason = str(silence_exp.get("required_reason_code") or "")
    reason_count = sum(1 for x in silence if x.get("reason_code") == reason)
    silence_ok = (
        len(silence) == int(silence_exp.get("active_media_candidate_count") or -1)
        and promoted == int(silence_exp.get("promoted_count") or -1)
        and suppressed == int(silence_exp.get("suppressed_count") or -1)
        and reason_count == int(silence_exp.get("suppressed_count") or -1)
    )
    _check(checks, "PCM_TX_SILENCE_DECISIONS", silence_ok, {
        "expected": silence_exp,
        "actual": {
            "candidate_count": len(silence),
            "promoted_count": promoted,
            "suppressed_count": suppressed,
            "required_reason_count": reason_count,
            "decisions": silence,
        },
    })

    corr_exp = expected.get("pcm_tx_rtp_correlation") or {}
    corr = _best_pcm_rtp_correlation(media, tap_name="pcm_tx")
    corr_ok = bool(corr)
    if corr_ok:
        corr_ok = (
            corr["absolute_correlation"] >= float(corr_exp.get("minimum_absolute_correlation") or 0.0)
            and corr.get("quality") == corr_exp.get("expected_quality")
            and abs(corr["lag_ms"] - float(corr_exp.get("expected_lag_ms") or 0.0))
                <= float(corr_exp.get("lag_tolerance_ms") or 0.0)
        )
    _check(checks, "PCM_TX_RTP_CORRELATION", corr_ok, {
        "expected": corr_exp,
        "actual": corr,
    })

    packet_exp = expected.get("packet_invariants") or {}
    max_loss = float(packet_exp.get("maximum_rtp_loss_rate") or 0.0)
    stream_losses = [float(s.get("loss_rate") or 0.0) for s in packet.get("rtp_streams", []) or []]
    packet_ok = bool(stream_losses) and max(stream_losses) <= max_loss
    _check(checks, "RTP_LOSS_INVARIANT", packet_ok, {
        "maximum_allowed": max_loss,
        "actual_loss_rates": stream_losses,
    })

    findings = compose_findings(packet=packet, pcm=pcm, media=media, source_run_ids={})
    finding_types = [str(f.get("type") or "") for f in findings]
    report_exp = expected.get("report_invariants") or {}
    forbidden = set(report_exp.get("must_not_contain_finding_types") or [])
    required_any = set(report_exp.get("must_contain_any_finding_types") or [])
    _check(checks, "REPORT_FORBIDDEN_FINDINGS", not (forbidden & set(finding_types)), {
        "forbidden": sorted(forbidden), "actual": finding_types,
    })
    _check(checks, "REPORT_REQUIRED_MAIN_FINDING", bool(required_any & set(finding_types)), {
        "required_any": sorted(required_any), "actual": finding_types,
    })

    status = "PASS" if all(x["status"] == "PASS" for x in checks) else "FAIL"
    return {
        "schema_version": "pr2-field-candidate-gate-v1",
        "contract_id": contract.get("id"),
        "status": status,
        "source": {"path": str(pcap), "sha256": actual_sha, "size_bytes": actual_size},
        "analyzer": {
            "media_version": media.get("version"),
            "analyzer_profile": analyzer_profile,
            "pcm_profile": media.get("pcm_profile"),
        },
        "checks": checks,
        "summary": {
            "finding_types": finding_types,
            "pcm_tx_silence_candidate_count": len(silence),
            "pcm_tx_silence_promoted_count": promoted,
            "pcm_tx_silence_suppressed_count": suppressed,
            "pcm_tx_best_rtp_correlation": corr,
        },
        "boundary": "Field Golden validates PR2 false-positive behavior only; it does not confirm physical root cause or replace the full production Golden Dataset.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcap", type=Path, required=True)
    ap.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    ap.add_argument("--pcm-profile", type=Path, default=DEFAULT_PCM_PROFILE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--work-dir", type=Path)
    args = ap.parse_args()

    if not args.pcap.is_file():
        raise SystemExit(f"PCAP_NOT_FOUND:{args.pcap}")
    if not args.contract.is_file():
        raise SystemExit(f"CONTRACT_NOT_FOUND:{args.contract}")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))

    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        result = evaluate(args.pcap, contract, args.pcm_profile, args.work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="pr2-field-gate-") as tmp:
            result = evaluate(args.pcap, contract, args.pcm_profile, Path(tmp))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "contract_id": result.get("contract_id"), "out": str(args.out)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

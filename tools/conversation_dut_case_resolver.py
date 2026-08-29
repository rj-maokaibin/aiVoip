#!/usr/bin/env python3
"""Resolve one dedicated Conversation→DUT live-acceptance Case by tag.

This helper is intentionally read-only. It allows the live observer to be started
before a human sends the Feishu acceptance incident, then waits for exactly one
ACTIVE FeishuCaseBinding whose authoritative source text contains the explicit
acceptance tag. It never falls back to the newest Case or newest binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models import Case, FeishuCaseBinding
from app.db.session import SessionLocal
from tools.conversation_dut_live_acceptance import validate_acceptance_tag

CONTRACT = "conversation-dut-case-resolution-v1"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matching_bindings(rows: list[FeishuCaseBinding], tag: str) -> list[FeishuCaseBinding]:
    return [
        row
        for row in rows
        if tag in str(getattr(row, "source_normalized_text", "") or "")
        and str(getattr(row, "source_message_id", "") or "").startswith("om_")
    ]


def resolve_once(db, *, acceptance_tag: str) -> dict[str, Any]:
    tag = validate_acceptance_tag(acceptance_tag)
    rows = list(
        db.scalars(
            select(FeishuCaseBinding)
            .where(FeishuCaseBinding.status == "ACTIVE")
            .order_by(FeishuCaseBinding.created_at.desc())
        )
    )
    matches = _matching_bindings(rows, tag)
    case_ids = sorted({str(row.case_id) for row in matches if getattr(row, "case_id", None)})

    if not matches:
        return {
            "contract": CONTRACT,
            "status": "WAITING",
            "phase": "WAITING_FEISHU_CASE",
            "acceptance_tag_sha256": _sha256(tag),
            "match_count": 0,
            "safety": {"read_only": True, "fallback_to_recent_case": False},
        }
    if len(matches) != 1 or len(case_ids) != 1:
        return {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "phase": "BLOCKED",
            "blocker_codes": ["ACCEPTANCE_TAG_NOT_UNIQUE"],
            "acceptance_tag_sha256": _sha256(tag),
            "match_count": len(matches),
            "case_count": len(case_ids),
            "safety": {"read_only": True, "fallback_to_recent_case": False},
        }

    case = db.get(Case, case_ids[0])
    if case is None or not str(case.case_no or "").startswith("VOIP-"):
        return {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "phase": "BLOCKED",
            "blocker_codes": ["MATCHED_CASE_INVALID"],
            "acceptance_tag_sha256": _sha256(tag),
            "match_count": len(matches),
            "safety": {"read_only": True, "fallback_to_recent_case": False},
        }

    binding = matches[0]
    return {
        "contract": CONTRACT,
        "status": "PASS",
        "phase": "CASE_RESOLVED",
        "case_no": case.case_no,
        "case_id": case.id,
        "binding_id": binding.id,
        "source_message_sha256": _sha256(str(binding.source_message_id)),
        "acceptance_tag_sha256": _sha256(tag),
        "match_count": 1,
        "case_count": 1,
        "safety": {"read_only": True, "fallback_to_recent_case": False},
    }


def wait_for_case(*, acceptance_tag: str, wait_seconds: int, poll_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    last: dict[str, Any] | None = None
    while True:
        with SessionLocal() as db:
            last = resolve_once(db, acceptance_tag=acceptance_tag)
        if last.get("status") in {"PASS", "BLOCKED"}:
            return last
        if time.monotonic() >= deadline:
            return {
                **last,
                "status": "BLOCKED",
                "phase": "BLOCKED",
                "blocker_codes": ["CASE_RESOLUTION_TIMEOUT"],
                "error": "CASE_RESOLUTION_TIMEOUT",
            }
        time.sleep(max(1, int(poll_seconds)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve dedicated Conversation DUT acceptance Case by tag")
    parser.add_argument("--acceptance-tag", required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path("validation/conversation_dut_case_resolution_v1.json"))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    try:
        payload = wait_for_case(
            acceptance_tag=args.acceptance_tag,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
    except Exception as exc:
        payload = {
            "contract": CONTRACT,
            "status": "BLOCKED",
            "phase": "BLOCKED",
            "error": type(exc).__name__,
            "error_message": str(exc)[:300],
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "contract": payload.get("contract"),
        "status": payload.get("status"),
        "phase": payload.get("phase"),
        "case_no": payload.get("case_no"),
        "blocker_codes": payload.get("blocker_codes", []),
        "out": str(args.out),
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and payload.get("status") != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())

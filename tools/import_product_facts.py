#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.knowledge.importer import import_product_fact_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Import structured VOIP ProductFact data")
    parser.add_argument("path", help="JSON/YAML/CSV file or directory")
    parser.add_argument("--actor", default="knowledge-import-cli")
    parser.add_argument("--approval", choices=["DRAFT", "REVIEW", "APPROVED"], default="DRAFT")
    parser.add_argument("--no-update", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = import_product_fact_path(
            db,
            args.path,
            actor=args.actor,
            default_approval=args.approval,
            allow_update=not args.no_update,
        )
        payload = {
            "created": result.created,
            "updated": result.updated,
            "skipped": result.skipped,
            "ids": result.ids,
            "errors": result.errors,
        }
        if result.errors and not args.allow_partial:
            db.rollback()
            payload["status"] = "FAILED_ROLLED_BACK"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        db.commit()
        payload["status"] = "IMPORTED_WITH_ERRORS" if result.errors else "IMPORTED"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not result.errors else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

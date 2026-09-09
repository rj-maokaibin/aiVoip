#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
import app.db.models  # noqa: F401
import app.automation.pbx_models  # noqa: F401
from app.automation.adapters.pbx.profile import load_fusionpbx_profile
from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence
from app.automation.adapters.pbx.inventory import PbxInventoryService
from app.automation.adapters.pbx.resource_authority import PbxExtensionLeaseManager
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.adapters.pbx.resource_manager import PbxResourceManager

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run controlled G-PBX-001 extension lifecycle")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--extension", default=None, help="Optional managed extension identity")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = load_fusionpbx_profile(args.profile)
    config = FusionPbxConfigProvider(profile)
    fence = FusionPbxSourceFence(profile)
    runtime = FreeSwitchRuntimeReadProbe(profile)
    domains = config.discover_domains()
    if len(domains) != 1:
        raise RuntimeError("PBX_DOMAIN_CARDINALITY_INVALID")
    domain = domains[0]

    state_path = Path(args.state_db).resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{state_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    inventory = PbxInventoryService(
        Session,
        profile,
        config_provider=config,
        source_fence=fence,
    ).sync()
    authority = PbxExtensionLeaseManager(Session, ttl_seconds=180)
    if args.extension:
        resource = PbxInventoryService(
            Session, profile, config_provider=config, source_fence=fence
        ).ensure_automation_identity(
            pbx_node_id=inventory["node_id"],
            domain_id=domain.domain_id,
            extension=args.extension,
        )
        token = authority.acquire_extension(
            pbx_node_id=inventory["node_id"], extension=resource.extension,
            run_id=args.run_id, owner_worker_id=args.owner,
        )
    else:
        token = authority.acquire_first_available(
            pbx_node_id=inventory["node_id"],
            run_id=args.run_id,
            owner_worker_id=args.owner,
        )
    mutation = FusionPbxMutationProvider(
        profile,
        authority=authority,
        source_fence=fence,
        timeout_seconds=20,
    )
    manager = PbxResourceManager(
        Session,
        authority=authority,
        config=config,
        mutation=mutation,
        runtime=runtime,
    )

    credential = secrets.token_urlsafe(24)
    evidence = {
        "schema": "g-pbx-001-v1",
        "extension": token.extension,
        "lease_epoch": token.lease_epoch,
        "source_fence": True,
        "mutation_entry": "FusionPBX-native-provider",
        "secret_values_emitted": False,
    }
    cleanup_ok = False
    try:
        evidence["before_exists"] = config.extension_exists(token.extension)
        evidence["before_runtime_visible"] = runtime.user_visible(
            token.extension,
            domain_name=domain.domain_name,
        )
        created = manager.provision_extension(
            token,
            password=credential,
            template_identity="7102",
        )
        evidence["create"] = created.safe_dict()
        evidence["after_create_exists"] = config.extension_exists(token.extension)
        evidence["after_create_runtime_visible"] = runtime.user_visible(
            token.extension,
            domain_name=domain.domain_name,
        )
    except Exception as exc:
        evidence["create_error_code"] = getattr(exc, "code", type(exc).__name__)
    finally:
        try:
            cleaned = manager.deprovision_extension(token)
            evidence["cleanup"] = cleaned.safe_dict()
            evidence["after_cleanup_exists"] = config.extension_exists(token.extension)
            evidence["after_cleanup_runtime_visible"] = runtime.user_visible(
                token.extension,
                domain_name=domain.domain_name,
            )
            cleanup_ok = (
                evidence["after_cleanup_exists"] is False
                and evidence["after_cleanup_runtime_visible"] is False
            )
        except Exception as exc:
            evidence["cleanup_error_code"] = getattr(exc, "code", type(exc).__name__)

    if cleanup_ok:
        manager.release_extension(token)
        evidence["release_last"] = True
    else:
        evidence["release_last"] = False

    evidence["7102_still_exists"] = config.extension_exists("7102")
    evidence["7102_runtime_visible"] = runtime.user_visible(
        "7102",
        domain_name=domain.domain_name,
    )
    evidence["verdict"] = "PASS" if (
        evidence.get("before_exists") is False
        and evidence.get("before_runtime_visible") is False
        and evidence.get("after_create_exists") is True
        and evidence.get("after_create_runtime_visible") is True
        and cleanup_ok
        and evidence.get("release_last") is True
        and evidence["7102_still_exists"] is True
        and evidence["7102_runtime_visible"] is True
    ) else "FAIL"

    safe_json = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
    Path(args.evidence).write_text(
        json.dumps(evidence, sort_keys=True, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(safe_json)
    return 0 if evidence["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

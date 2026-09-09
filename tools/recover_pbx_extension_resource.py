#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
import app.db.models  # noqa: F401
import app.automation.pbx_models  # noqa: F401
from app.automation.pbx_models import PbxExtensionResource, PbxNode
from app.automation.adapters.pbx.profile import load_fusionpbx_profile
from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence
from app.automation.adapters.pbx.resource_authority import PbxExtensionLeaseManager
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.adapters.pbx.resource_manager import PbxResourceManager


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--state-db", required=True)
    p.add_argument("--extension", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--owner", required=True)
    return p.parse_args()


def main() -> int:
    a = args()
    profile = load_fusionpbx_profile(a.profile)
    config = FusionPbxConfigProvider(profile)
    fence = FusionPbxSourceFence(profile)
    runtime = FreeSwitchRuntimeReadProbe(profile)
    engine = create_engine(f"sqlite+pysqlite:///{a.state_db}")
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as session:
        row = session.execute(
            select(PbxExtensionResource).where(
                PbxExtensionResource.extension == a.extension,
            )
        ).scalar_one()
        node = session.get(PbxNode, row.pbx_node_id)
        node_id = node.id

    authority = PbxExtensionLeaseManager(Session, ttl_seconds=180)
    try:
        token = authority.resume_active_lease(
            pbx_node_id=node_id, extension=a.extension, run_id=a.run_id, owner_worker_id=a.owner
        )
    except Exception as exc:
        if getattr(exc, "code", "") != "PBX_LEASE_EXPIRED":
            raise
        token = authority.acquire_recovery(
            pbx_node_id=node_id, extension=a.extension,
            run_id=f"{a.run_id}-recovery", owner_worker_id=a.owner,
        )
    mutation = FusionPbxMutationProvider(
        profile,
        authority=authority,
        source_fence=fence,
    )
    manager = PbxResourceManager(
        Session,
        authority=authority,
        config=config,
        mutation=mutation,
        runtime=runtime,
    )
    result = manager.deprovision_extension(token)
    manager.release_extension(token)
    domain = config.discover_domains()[0]
    evidence = {
        "schema": "pbx-resource-recovery-v1",
        "extension": a.extension,
        "cleanup": result.safe_dict(),
        "pbx_exists": config.extension_exists(a.extension),
        "runtime_visible": runtime.user_visible(a.extension, domain_name=domain.domain_name),
        "lease_released": authority.validate(token) is False,
        "secret_values_emitted": False,
    }
    evidence["verdict"] = "PASS" if (
        evidence["pbx_exists"] is False
        and evidence["runtime_visible"] is False
        and evidence["lease_released"] is True
    ) else "FAIL"
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

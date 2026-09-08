from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxDomain
from app.automation.adapters.pbx.inventory import PbxInventoryService
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import (
    PbxExtensionLeaseManager,
    PbxResourceAuthorityError,
    PbxResourceState,
    PbxResourceType,
)
from app.automation.pbx_models import PbxExtensionResource, PbxNode, PbxResourceLease
from app.db.base import Base


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _profile() -> FusionPbxLabProfile:
    return FusionPbxLabProfile.from_dict(
        {
            "schema_version": "pbx-lab-profile-v1",
            "node_key": "test-pbx",
            "host": "127.0.0.1",
            "fusionpbx_root": "/tmp/fusionpbx",
            "php_bin": "/usr/bin/php",
            "fs_cli_bin": "/usr/bin/fs_cli",
            "internal_profile": "internal",
            "internal_port": 5060,
            "extension_pool": {"start": 7900, "end": 7902},
            "protected_extensions": ["7102"],
            "source_fence": {"version": "test-v1", "files": {"x": "0" * 64}},
        }
    )


def test_inventory_protects_existing_and_seeds_absent_pool() -> None:
    Session = _session_factory()

    class Config:
        def discover_domains(self):
            return (FusionPbxDomain(domain_id="d1", domain_name="pbx.test"),)

        def list_identities(self):
            return {"d1": {"7102", "7901"}}

    service = PbxInventoryService(
        Session,
        _profile(),
        config_provider=Config(),
        source_fence=SimpleNamespace(verify=lambda: SimpleNamespace(version="test-v1")),
    )
    evidence = service.sync()
    assert evidence["mutation_executed"] is False
    with Session() as session:
        rows = {
            row.extension: row
            for row in session.execute(select(PbxExtensionResource)).scalars()
        }
        assert rows["7102"].resource_type == PbxResourceType.STATIC_BASELINE.value
        assert rows["7102"].deletable is False
        assert rows["7901"].resource_type == PbxResourceType.STATIC_BASELINE.value
        assert rows["7901"].deletable is False
        assert rows["7900"].resource_type == PbxResourceType.TEMPORARY_AUTOMATION.value
        assert rows["7900"].state == PbxResourceState.FREE.value
        assert rows["7900"].deletable is False


def test_lease_is_exclusive_idempotent_and_epoch_fenced() -> None:
    Session = _session_factory()
    with Session() as session:
        node = PbxNode(
            node_key="test-pbx",
            provider="fusionpbx",
            runtime_provider="freeswitch",
            host="127.0.0.1",
            fusionpbx_root="/tmp/fusionpbx",
            internal_profile="internal",
            internal_port=5060,
            status="PBX_READY",
            source_fence_version="test-v1",
        )
        session.add(node)
        session.flush()
        session.add_all(
            [
                PbxExtensionResource(
                    pbx_node_id=node.id,
                    domain_id="d1",
                    extension="7102",
                    resource_type=PbxResourceType.STATIC_BASELINE.value,
                    state=PbxResourceState.READY.value,
                    deletable=False,
                ),
                PbxExtensionResource(
                    pbx_node_id=node.id,
                    domain_id="d1",
                    extension="7900",
                    resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
                    state=PbxResourceState.FREE.value,
                    deletable=False,
                ),
            ]
        )
        session.commit()
        node_id = node.id

    manager = PbxExtensionLeaseManager(Session, ttl_seconds=60)
    with pytest.raises(PbxResourceAuthorityError, match="PBX_RESOURCE_PROTECTED"):
        manager.acquire_extension(
            pbx_node_id=node_id,
            extension="7102",
            run_id="run-a",
            owner_worker_id="worker-a",
        )

    token1 = manager.acquire_extension(
        pbx_node_id=node_id,
        extension="7900",
        run_id="run-a",
        owner_worker_id="worker-a",
    )
    same = manager.acquire_extension(
        pbx_node_id=node_id,
        extension="7900",
        run_id="run-a",
        owner_worker_id="worker-a",
    )
    assert same.lease_id == token1.lease_id
    assert same.lease_epoch == 1
    assert manager.validate(token1) is True

    with pytest.raises(PbxResourceAuthorityError, match="PBX_RESOURCE_BUSY"):
        manager.acquire_extension(
            pbx_node_id=node_id,
            extension="7900",
            run_id="run-b",
            owner_worker_id="worker-b",
        )

    with Session() as session:
        row = session.get(PbxExtensionResource, token1.resource_id)
        row.state = PbxResourceState.FREE.value
        session.commit()
    manager.release(token1)
    assert manager.validate(token1) is False

    token2 = manager.acquire_extension(
        pbx_node_id=node_id,
        extension="7900",
        run_id="run-b",
        owner_worker_id="worker-b",
    )
    assert token2.lease_epoch == 2
    assert token2.lease_id != token1.lease_id
    assert manager.validate(token1) is False
    assert manager.validate(token2) is True


def test_release_is_fail_closed_until_cleanup_marks_free() -> None:
    Session = _session_factory()
    with Session() as session:
        node = PbxNode(
            node_key="test-pbx",
            provider="fusionpbx",
            runtime_provider="freeswitch",
            host="127.0.0.1",
            fusionpbx_root="/tmp/fusionpbx",
            internal_profile="internal",
            internal_port=5060,
            status="PBX_READY",
            source_fence_version="test-v1",
        )
        session.add(node)
        session.flush()
        resource = PbxExtensionResource(
            pbx_node_id=node.id,
            domain_id="d1",
            extension="7900",
            resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
            state=PbxResourceState.FREE.value,
        )
        session.add(resource)
        session.commit()
        node_id = node.id

    manager = PbxExtensionLeaseManager(Session)
    token = manager.acquire_extension(
        pbx_node_id=node_id,
        extension="7900",
        run_id="run-a",
        owner_worker_id="worker-a",
    )
    with pytest.raises(PbxResourceAuthorityError, match="PBX_RESOURCE_CLEANUP_REQUIRED"):
        manager.release(token)
    with Session() as session:
        lease = session.get(PbxResourceLease, token.lease_id)
        assert lease.state == "ACTIVE"

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxDomain, FusionPbxExtensionView
from app.automation.adapters.pbx.fusionpbx_mutation import (
    FusionPbxMutationError,
    FusionPbxMutationProvider,
    FusionPbxMutationResult,
)
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import (
    PbxExtensionLeaseManager,
    PbxResourceState,
    PbxResourceType,
)
from app.automation.adapters.pbx.resource_manager import PbxResourceManager, PbxResourceManagerError
from app.automation.pbx_models import PbxExtensionResource, PbxMutationSnapshot, PbxNode
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
    return FusionPbxLabProfile.from_dict({
        "schema_version": "pbx-lab-profile-v1",
        "node_key": "test-pbx",
        "host": "127.0.0.1",
        "fusionpbx_root": "/tmp/fusionpbx",
        "php_bin": "/usr/bin/php",
        "fs_cli_bin": "/usr/bin/fs_cli",
        "internal_profile": "internal",
        "internal_port": 5060,
        "extension_pool": {"start": 7900, "end": 7999},
        "protected_extensions": ["7102"],
        "source_fence": {"version": "test-v1", "files": {"x": "0" * 64}},
    })

def _seed(Session):
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
            provider_metadata_json={"domains": {"d1": "pbx.test"}},
        )
        session.add(node)
        session.flush()
        resource = PbxExtensionResource(
            pbx_node_id=node.id,
            domain_id="d1",
            extension="7900",
            resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
            state=PbxResourceState.FREE.value,
            deletable=False,
        )
        session.add(resource)
        session.commit()
        return node.id, resource.id


class FakeConfig:
    def __init__(self):
        self.view: FusionPbxExtensionView | None = None

    def discover_domains(self):
        return (FusionPbxDomain(domain_id="d1", domain_name="pbx.test"),)

    def get_extension(self, identity: str):
        if self.view is None or identity not in {self.view.extension, self.view.number_alias}:
            return ()
        return (self.view,)


class FakeRuntime:
    def __init__(self, config: FakeConfig):
        self.config = config

    def user_visible(self, identity: str, *, domain_name: str) -> bool:
        return bool(
            self.config.view
            and self.config.view.extension == identity
            and domain_name == "pbx.test"
        )

class FakeMutation:
    def __init__(
        self,
        config: FakeConfig,
        *,
        create_status="CONFIRMED",
        delete_status="CONFIRMED",
        apply_create=True,
        apply_delete=True,
    ):
        self.config = config
        self.create_status = create_status
        self.delete_status = delete_status
        self.apply_create = apply_create
        self.apply_delete = apply_delete
        self.create_calls = 0
        self.delete_calls = 0

    def create_extension(self, token, *, domain_name: str, password: str, template_identity: str):
        self.create_calls += 1
        marker = f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"
        if self.apply_create:
            self.config.view = FusionPbxExtensionView(
                domain_id=token.domain_id,
                extension_uuid="uuid-7900",
                extension=token.extension,
                number_alias=None,
                enabled=True,                description=marker,
                accountcode=token.extension,
                user_context="default",
                password_present=True,
            )
        return FusionPbxMutationResult(
            self.create_status,
            "create",
            token.extension,
            "uuid-7900",
            marker,
            True,
        )

    def delete_extension(self, token, *, domain_name: str):
        self.delete_calls += 1
        marker = f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"
        if self.apply_delete:
            self.config.view = None
        return FusionPbxMutationResult(
            self.delete_status,
            "delete",
            token.extension,
            "uuid-7900",
            marker,
            True,
        )

def _manager(
    *,
    create_status="CONFIRMED",
    delete_status="CONFIRMED",
    apply_create=True,
    apply_delete=True,
):
    Session = _session_factory()
    node_id, _ = _seed(Session)
    authority = PbxExtensionLeaseManager(Session, ttl_seconds=60)
    token = authority.acquire_extension(
        pbx_node_id=node_id,
        extension="7900",
        run_id="run-1",
        owner_worker_id="worker-1",
    )
    config = FakeConfig()
    runtime = FakeRuntime(config)
    mutation = FakeMutation(
        config,
        create_status=create_status,
        delete_status=delete_status,
        apply_create=apply_create,
        apply_delete=apply_delete,
    )
    manager = PbxResourceManager(
        Session,
        authority=authority,
        config=config,
        mutation=mutation,
        runtime=runtime,
    )
    return Session, authority, token, config, mutation, manager

def test_full_extension_lifecycle_is_lease_owned_and_release_last() -> None:
    Session, authority, token, config, mutation, manager = _manager()
    result = manager.provision_extension(
        token,
        password="unit-password-123",
        template_identity="7102",
    )
    assert result.status == "CONFIRMED"
    assert result.runtime_visible is True
    assert mutation.create_calls == 1
    with Session() as session:
        row = session.get(PbxExtensionResource, token.resource_id)
        assert row.state == PbxResourceState.READY.value
        assert row.created_by_automation is True
        assert row.deletable is True
        assert row.ownership_marker == "AIVOIP_AUTOMATION:run-1:1"
        snapshots = session.execute(select(PbxMutationSnapshot)).scalars().all()
        assert snapshots[0].snapshot_json == {"extension": "7900", "exists": False}
        assert "unit-password-123" not in repr(row.__dict__)

    cleanup = manager.deprovision_extension(token)
    assert cleanup.status == "CONFIRMED"
    assert mutation.delete_calls == 1
    assert authority.validate(token) is True
    manager.release_extension(token)
    assert authority.validate(token) is False

def test_unknown_create_is_observed_once_and_never_retried() -> None:
    _, _, token, _, mutation, manager = _manager(
        create_status="UNKNOWN",
        apply_create=True,
    )
    result = manager.provision_extension(token, password="unit-password-123")
    assert result.status == "CONFIRMED_BY_OBSERVATION"
    assert result.observed_after_unknown is True
    assert mutation.create_calls == 1


def test_unknown_create_without_readback_goes_dirty_without_retry() -> None:
    Session, _, token, _, mutation, manager = _manager(
        create_status="UNKNOWN",
        apply_create=False,
    )
    with pytest.raises(PbxResourceManagerError, match="PBX_MUTATION_UNKNOWN"):
        manager.provision_extension(token, password="unit-password-123")
    assert mutation.create_calls == 1
    with Session() as session:
        row = session.get(PbxExtensionResource, token.resource_id)
        assert row.state == PbxResourceState.DIRTY.value

def test_cleanup_refuses_ownership_mismatch_without_delete() -> None:
    Session, _, token, config, mutation, manager = _manager()
    manager.provision_extension(token, password="unit-password-123")
    config.view = replace(config.view, description="MANUAL_RESOURCE")
    with pytest.raises(
        PbxResourceManagerError,
        match="PBX_RESOURCE_OWNERSHIP_UNPROVEN",
    ):
        manager.deprovision_extension(token)
    assert mutation.delete_calls == 0
    with Session() as session:
        row = session.get(PbxExtensionResource, token.resource_id)
        assert row.state == PbxResourceState.DIRTY.value


def test_unknown_delete_observes_absence_and_does_not_retry() -> None:
    _, authority, token, _, mutation, manager = _manager(
        delete_status="UNKNOWN",
        apply_delete=True,
    )
    manager.provision_extension(token, password="unit-password-123")
    result = manager.deprovision_extension(token)
    assert result.status == "CONFIRMED_BY_OBSERVATION"
    assert mutation.delete_calls == 1
    assert authority.validate(token) is True

def test_mutation_provider_enforces_token_fence_and_redacts_password() -> None:
    profile = _profile()
    token = SimpleNamespace(
        lease_id="l1",
        resource_id="r1",
        pbx_node_id="n1",
        domain_id="d1",
        extension="7900",
        run_id="run-1",
        owner_worker_id="worker-1",
        lease_epoch=7,
    )
    calls = []

    def runner(script, req, _timeout):
        calls.append((script, dict(req)))
        return 0, (
            '{"status":"CONFIRMED","operation":"create",'
            '"extension":"7900","extension_uuid":"u1",'
            '"ownership_marker":"AIVOIP_AUTOMATION:run-1:7",'
            '"runtime_reload_requested":true,"secret_values_emitted":false}'
        )

    provider = FusionPbxMutationProvider(
        profile,
        authority=SimpleNamespace(validate=lambda _t: True),
        source_fence=SimpleNamespace(verify_mutation_contract=lambda: SimpleNamespace(ok=True)),
        runner=runner,
    )
    result = provider.create_extension(
        token,
        domain_name="pbx.test",
        password="unit-password-123",
        template_identity="7102",
    )
    assert result.confirmed is True
    assert len(calls) == 1
    assert calls[0][1]["password"] == "unit-password-123"
    assert "unit-password-123" not in repr(result.safe_dict())

    fenced = FusionPbxMutationProvider(
        profile,
        authority=SimpleNamespace(validate=lambda _t: False),
        source_fence=SimpleNamespace(verify_mutation_contract=lambda: SimpleNamespace(ok=True)),
        runner=lambda *_a: pytest.fail("fenced mutation must not invoke provider"),
    )
    with pytest.raises(FusionPbxMutationError, match="PBX_LEASE_FENCED"):
        fenced.create_extension(
            token,
            domain_name="pbx.test",
            password="unit-password-123",
            template_identity="7102",
        )

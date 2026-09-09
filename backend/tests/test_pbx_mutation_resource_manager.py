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

def _seed(Session, *, extension: str = "7900"):
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
            extension=extension,
            resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
            state=PbxResourceState.FREE.value,
            deletable=False,
        )
        session.add(resource)
        if extension != "7900":
            session.add(PbxExtensionResource(
                pbx_node_id=node.id, domain_id="d1", extension="7900",
                resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
                state=PbxResourceState.FREE.value, deletable=False,
            ))
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
        self.stale_visible = False

    def user_visible(self, identity: str, *, domain_name: str) -> bool:
        return bool(
            self.stale_visible
            or (self.config.view and identity in {self.config.view.extension, self.config.view.number_alias} and domain_name == "pbx.test")
        )

    def user_context(self, identity: str, *, domain_name: str) -> str | None:
        return "default" if self.stale_visible else None

    def resolve_directory_identity(self, identity: str, *, domain_name: str):
        if self.config.view is None or domain_name != "pbx.test":
            return None
        if identity not in {self.config.view.extension, self.config.view.number_alias}:
            return None
        return {
            "resolved_identity": self.config.view.extension,
            "number_alias": self.config.view.number_alias,
            "domain_name": domain_name,
            "secret_values_emitted": False,
        }

class FakeMutation:
    def __init__(
        self,
        config: FakeConfig,
        *,
        create_status="CONFIRMED",
        delete_status="CONFIRMED",
        apply_create=True,
        apply_delete=True,
        runtime=None,
    ):
        self.config = config
        self.runtime = runtime
        self.create_status = create_status
        self.delete_status = delete_status
        self.apply_create = apply_create
        self.apply_delete = apply_delete
        self.create_calls = 0
        self.delete_calls = 0
        self.reconcile_calls = 0
        self.last_create_alias = None
        self.last_reconcile_alias = None

    def create_extension(self, token, *, domain_name: str, password: str, template_identity: str, dial_alias: str | None = None):
        self.create_calls += 1
        self.last_create_alias = dial_alias
        marker = f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"
        if self.apply_create:
            self.config.view = FusionPbxExtensionView(
                domain_id=token.domain_id,
                extension_uuid="uuid-7900",
                extension=token.extension,
                number_alias=dial_alias,
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

    def delete_extension(self, token, *, domain_name: str, ownership_marker: str | None = None):
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
    def reconcile_absent_extension_cache(
        self, token, *, domain_name: str, user_context: str, dial_alias: str | None = None, ownership_marker: str | None = None
    ):
        self.reconcile_calls += 1
        self.last_reconcile_alias = dial_alias
        if self.runtime is not None:
            self.runtime.stale_visible = False
        marker = ownership_marker or f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"
        return FusionPbxMutationResult(
            "CONFIRMED", "reconcile_absent_cache", token.extension, None, marker, True
        )


def _manager(
    *,
    create_status="CONFIRMED",
    delete_status="CONFIRMED",
    apply_create=True,
    apply_delete=True,
    extension="7900",
    dial_alias: str | None = None,
):
    Session = _session_factory()
    node_id, _ = _seed(Session, extension=extension)
    authority = PbxExtensionLeaseManager(Session, ttl_seconds=60)
    token = authority.acquire_endpoint(
        pbx_node_id=node_id, extension=extension, dial_alias=dial_alias,
        run_id="run-1", owner_worker_id="worker-1",
    )
    config = FakeConfig()
    runtime = FakeRuntime(config)
    mutation = FakeMutation(
        config,
        create_status=create_status,
        delete_status=delete_status,
        apply_create=apply_create,
        apply_delete=apply_delete,
        runtime=runtime,
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
        assert snapshots[0].snapshot_json == {"extension": "7900", "dial_alias": None, "exists": False}
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
    assert "$cache->delete('directory:' . $extension . '@' . $domain_name);" in calls[0][0]
    assert "$cache->delete('directory:' . $extension . '@' . $user_context);" in calls[0][0]

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


def test_delete_with_stale_runtime_reconciles_cache_once_without_second_delete() -> None:
    _, authority, token, _, mutation, manager = _manager()
    manager.provision_extension(token, password="unit-password-123")
    manager.runtime.stale_visible = True
    result = manager.deprovision_extension(token)
    assert result.status == "CONFIRMED"
    assert manager.runtime.stale_visible is False
    assert mutation.delete_calls == 1
    assert mutation.reconcile_calls == 1
    assert authority.validate(token) is True


def test_absent_pbx_with_stale_runtime_reconciles_cache_once() -> None:
    Session, authority, token, config, mutation, manager = _manager()
    manager.provision_extension(token, password="unit-password-123")
    config.view = None
    manager.runtime.stale_visible = True
    result = manager.deprovision_extension(token)
    assert result.status == "CONFIRMED_BY_OBSERVATION"
    assert manager.runtime.stale_visible is False
    assert mutation.delete_calls == 0
    assert mutation.reconcile_calls == 1
    assert authority.validate(token) is True


def test_runtime_cache_reconcile_uses_domain_and_context_under_same_lease() -> None:
    profile = _profile()
    token = SimpleNamespace(
        lease_id="l1", resource_id="r1", pbx_node_id="n1", domain_id="d1",
        extension="7900", run_id="recovery-run", owner_worker_id="worker-1", lease_epoch=9,
    )
    cache_calls = []
    provider = FusionPbxMutationProvider(
        profile,
        authority=SimpleNamespace(validate=lambda _t: True),
        source_fence=SimpleNamespace(verify_mutation_contract=lambda: SimpleNamespace(ok=True)),
        runner=lambda *_a: pytest.fail("cache reconcile must not use PHP mutation runner"),
        cache_reconcile_runner=lambda keys, _timeout: cache_calls.append(keys) or True,
    )
    marker = "AIVOIP_AUTOMATION:original-run:1"
    result = provider.reconcile_absent_extension_cache(
        token, domain_name="pbx.test", user_context="default", ownership_marker=marker,
    )
    assert result.confirmed is True
    assert result.ownership_marker == marker
    assert cache_calls == [("directory:7900@pbx.test", "directory:7900@default")]


def test_non_numeric_identity_with_numeric_dial_alias_is_managed_as_one_resource() -> None:
    Session, authority, token, config, mutation, manager = _manager(extension="7900.a", dial_alias="7900")
    result = manager.provision_extension(
        token, password="unit-password-123", dial_alias="7900"
    )
    assert result.extension == "7900.a"
    assert result.dial_alias == "7900"
    assert result.runtime_visible is True
    assert config.view is not None and config.view.number_alias == "7900"
    assert mutation.last_create_alias == "7900"
    with Session() as session:
        row = session.get(PbxExtensionResource, token.resource_id)
        assert row.dial_alias == "7900"
        snapshot = session.execute(select(PbxMutationSnapshot)).scalar_one()
        assert snapshot.snapshot_json == {"extension": "7900.a", "dial_alias": "7900", "exists": False}
    cleanup = manager.deprovision_extension(token)
    assert cleanup.dial_alias == "7900"
    assert authority.validate(token) is True
    manager.release_extension(token)
    assert authority.validate(token) is False


def test_dial_alias_conflict_fails_before_mutation() -> None:
    _, _, token, config, mutation, manager = _manager(extension="7900.a", dial_alias="7900")
    config.view = FusionPbxExtensionView(
        domain_id="d1", extension_uuid="manual-1", extension="7109",
        number_alias="7900", enabled=True, description="MANUAL", accountcode="7109",
        user_context="default", password_present=True,
    )
    with pytest.raises(PbxResourceManagerError, match="PBX_DIAL_ALIAS_CONFLICT"):
        manager.provision_extension(token, password="unit-password-123", dial_alias="7900")
    assert mutation.create_calls == 0


def test_dial_alias_stale_runtime_is_reconciled_under_same_lease() -> None:
    _, authority, token, config, mutation, manager = _manager(extension="7900.a", dial_alias="7900")
    manager.provision_extension(token, password="unit-password-123", dial_alias="7900")
    config.view = None
    manager.runtime.stale_visible = True
    result = manager.deprovision_extension(token)
    assert result.status == "CONFIRMED_BY_OBSERVATION"
    assert mutation.delete_calls == 0
    assert mutation.reconcile_calls == 1
    assert mutation.last_reconcile_alias == "7900"
    assert authority.validate(token) is True


def test_provider_writes_number_alias_and_rejects_non_numeric_alias() -> None:
    profile = _profile()
    token = SimpleNamespace(
        lease_id="l1", resource_id="r1", pbx_node_id="n1", domain_id="d1",
        extension="7900.a", run_id="run-1", owner_worker_id="worker-1", lease_epoch=7,
    )
    calls = []
    def runner(script, req, _timeout):
        calls.append((script, dict(req)))
        return 0, ('{"status":"CONFIRMED","operation":"create","extension":"7900.a",'
                   '"extension_uuid":"u1","ownership_marker":"AIVOIP_AUTOMATION:run-1:7",'
                   '"runtime_reload_requested":true,"secret_values_emitted":false}')
    provider = FusionPbxMutationProvider(
        profile, authority=SimpleNamespace(validate=lambda _t: True),
        source_fence=SimpleNamespace(verify_mutation_contract=lambda: SimpleNamespace(ok=True)),
        runner=runner,
    )
    provider.create_extension(token, domain_name="pbx.test", password="unit-password-123",
                              template_identity="7102", dial_alias="7900")
    assert calls[0][1]["dial_alias"] == "7900"
    assert "number_alias" in calls[0][0] and "$dial_alias" in calls[0][0]
    with pytest.raises(FusionPbxMutationError, match="PBX_DIAL_ALIAS_INVALID"):
        provider.create_extension(token, domain_name="pbx.test", password="unit-password-123",
                                  template_identity="7102", dial_alias="7900.a")

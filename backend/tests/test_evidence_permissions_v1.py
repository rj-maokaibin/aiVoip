from app.api.evidence_permissions import EvidencePermission, has_evidence_permission
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole


def _identity(role: UserRole) -> AuthIdentity:
    return AuthIdentity(actor_id=f"test-{role.value.lower()}", role=role, authenticated=True, provider="test")


def test_viewer_can_view_report_but_cannot_download_raw_or_bundle():
    identity = _identity(UserRole.VIEWER)
    assert has_evidence_permission(identity, EvidencePermission.VIEW_REPORT)
    assert not has_evidence_permission(identity, EvidencePermission.VIEW_RAW_EVIDENCE)
    assert not has_evidence_permission(identity, EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE)
    assert not has_evidence_permission(identity, EvidencePermission.REBUILD_REPORT)


def test_engineer_can_read_raw_download_bundle_and_rebuild_but_not_manage_retention():
    identity = _identity(UserRole.ENGINEER)
    assert has_evidence_permission(identity, EvidencePermission.VIEW_REPORT)
    assert has_evidence_permission(identity, EvidencePermission.VIEW_RAW_EVIDENCE)
    assert has_evidence_permission(identity, EvidencePermission.DOWNLOAD_EVIDENCE_BUNDLE)
    assert has_evidence_permission(identity, EvidencePermission.REBUILD_REPORT)
    assert not has_evidence_permission(identity, EvidencePermission.MANAGE_RETENTION)


def test_reviewer_admin_and_service_can_manage_retention():
    for role in (UserRole.EXPERT_REVIEWER, UserRole.ADMIN, UserRole.SERVICE):
        assert has_evidence_permission(_identity(role), EvidencePermission.MANAGE_RETENTION)


def test_unknown_permission_is_fail_closed_for_viewer():
    identity = _identity(UserRole.VIEWER)
    allowed = [p for p in EvidencePermission if has_evidence_permission(identity, p)]
    assert allowed == [EvidencePermission.VIEW_REPORT]

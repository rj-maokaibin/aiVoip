# Feishu Case Gateway G3 — Document ACL Sync V1 Implementation Trace

## 1. Scope

G3 synchronizes the access policy of the single mutable Feishu Evidence Document for a Case with the Case source Feishu group. It is deterministic authorization infrastructure; AI does not decide or modify document permissions.

Stacking:

- G1: one group -> one Active Case
- G2: Feishu Identity + RBAC + Case ACL
- G3: this document ACL synchronization layer

## 2. Persistence

Migration: `0023_feishu_document_acl_v1`

Model: `FeishuDocumentAclBinding`

Persisted desired/applied state includes:

- Case / document / tenant / chat binding
- requested `sync_mode`
- `effective_mode`
- desired permission
- desired/applied revision
- status / retry count / last error / last sync time
- reconciliation metadata

The desired/applied revision contract makes synchronization idempotent and observable.

## 3. Supported synchronization modes

### CHAT_SCOPE

Primary mode. The source Feishu group is added to the docx collaborator list as an `openchat` collaborator. V1 default permission is `view`; ownership/full access is never granted implicitly.

### MEMBER_MIRROR

Fallback mode. The current source-group member open_ids are mirrored to document collaborators. Reconciliation performs add/update/remove deltas, including revoking members who have left the group.

### AUTO

Try CHAT_SCOPE first. If the live tenant/group does not permit that mode and fallback is enabled, fall back to MEMBER_MIRROR. The effective mode is persisted for audit/operations.

## 4. Integration points

`FeishuEvidenceDocumentService` remains the one-Case-one-Document projection owner.

After a successful Case document projection, `feishu.sync_document_acl` is queued. ACL failure is isolated from canonical Evidence/Report state: it may mark ACL projection failed/retrying, but it must never delete, invalidate, or rewrite the canonical report/evidence.

Celery worker uses bounded retry.

## 5. Management API

Admin/Service only, aligned with G2 `MANAGE_DOCUMENT_ACL` capability ownership:

- `GET /api/v1/cases/{case_id}/feishu-document-acl`
  - status
  - requested/effective mode
  - desired/applied revision
  - retry/error metadata
- `POST /api/v1/cases/{case_id}/feishu-document-acl/sync`
  - idempotent manual sync request
  - optional `force_revision=true`
  - audit record and task id

## 6. Security invariants

- AI does not participate in ACL decisions.
- Group/document identity is always tenant/chat/document scoped.
- G3 never changes document owner.
- G3 never grants `full_access` implicitly.
- Manual control endpoint is restricted to Admin/Service.
- ACL sync failure does not weaken canonical Evidence authorization.
- MEMBER_MIRROR removes stale users who leave the source group.

## 7. Tests

Focused tests cover:

- CHAT_SCOPE add and no-op idempotency
- permission update revision
- AUTO -> MEMBER_MIRROR fallback
- MEMBER_MIRROR add/remove reconciliation
- failure state does not affect Case/canonical assets
- status API before/after configuration
- manual sync idempotency
- force revision behavior

Repository CI additionally validates Python compile, AI E1-E6, M7, PostgreSQL migration chain, full backend regression, Preliminary Evidence Report software gate, frontend dependency audit and production build.

## 8. External acceptance gate

Software contract is fully testable with the adapter abstraction. Final production acceptance still requires a real Feishu tenant to verify the exact tenant permission grants, bot membership behavior, docx collaborator APIs, and group-member pagination under production credentials.

This external tenant verification is an environment gate, not permission to weaken or bypass G3 in production.

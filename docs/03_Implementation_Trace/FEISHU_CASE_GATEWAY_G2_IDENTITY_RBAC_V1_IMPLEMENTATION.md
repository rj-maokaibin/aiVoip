# Feishu Case Gateway G2 — Identity + RBAC V1 Implementation Trace

> Scope: **G2 / Feishu Identity + RBAC**  
> Baseline: `VOIP_AI_Intelligence_Layer_V1.0_方案与详细设计_编码实施版.md`  
> Stacked on: `agent/feishu-one-active-case-v1`  
> Status: **IMPLEMENTED / CI VALIDATION**

## 1. Design-change record: migration numbering

The coding plan originally grouped early Feishu-governance schema work around migration `0021`. PR-A/G1 has already frozen and validated:

```text
0021_feishu_case_governance_v1
```

An applied Alembic revision must never be edited later to smuggle new G2 tables into already-upgraded environments. Therefore G2 deliberately introduces:

```text
0022_feishu_identity_rbac_v1
```

and later G3/AI schema revisions move forward. This is a migration-safety correction only; G2 product semantics are unchanged.

## 2. Identity contract

Model:

```text
backend/app/db/feishu_governance_models.py
```

`FeishuUserIdentity` business key:

```text
tenant_key + open_id
```

Fields include:

- `internal_actor_id`
- `role` (`VIEWER / ENGINEER / EXPERT_REVIEWER / ADMIN / SERVICE`)
- `status` (`ACTIVE / DISABLED / PENDING_MAPPING`)
- optional `union_id / user_id / display_name`
- `last_seen_at`
- metadata and timestamps

Unknown users may be materialized as `PENDING_MAPPING` for Admin discovery, but they receive **no effective actor/role and no workflow authority**.

Same `open_id` in different tenants is isolated.

## 3. Capability authorization contract

Module:

```text
backend/app/api/feishu_permissions.py
```

Capabilities:

- `VIEW_CASE`
- `VIEW_REPORT`
- `VIEW_RAW_EVIDENCE`
- `DOWNLOAD_EVIDENCE_BUNDLE`
- `ADD_EVIDENCE`
- `REBUILD_REPORT`
- `CONTROL_REPRODUCTION`
- `COMPLETE_EXTERNAL_ACTION`
- `MARK_FIX_APPLIED`
- `RUN_REGISTERED_EXPERIMENT`
- `MANAGE_CASE_BINDING`
- `MANAGE_FEISHU_IDENTITY`
- `MANAGE_DOCUMENT_ACL`
- `MANAGE_RETENTION`
- `REVIEW_ROOT_CAUSE`

Effective decision order:

```text
Identity ACTIVE?
  no -> DENY
Global role contains capability?
  no -> DENY
Non-expired Case ACL DENY?
  yes -> DENY
otherwise -> ALLOW
```

A Case ACL `ALLOW` **cannot elevate beyond the global role**. Case Owner is an informational/UX overlay only in V1 and cannot upgrade a Viewer into control/review authority.

Each evaluated capability writes `AUTHORIZATION_DECIDED` audit with schema `feishu-authorization-v1`.

## 4. Intent-to-capability contract

| Intent / action | Capability |
|---|---|
| `NEW_DIAGNOSIS` | `ADD_EVIDENCE` |
| `CASE_FOLLOW_UP` | `ADD_EVIDENCE` |
| `STATUS_QUERY` | `VIEW_CASE` |
| `GENERAL_QUESTION` | `VIEW_CASE` |
| `STOP_REPRODUCTION` | `CONTROL_REPRODUCTION` |
| `EXTERNAL_ACTION_COMPLETED` | `COMPLETE_EXTERNAL_ACTION` |
| `FIX_APPLIED` | `MARK_FIX_APPLIED` |
| registered experiment | `RUN_REGISTERED_EXPERIMENT` |
| Bundle download | `DOWNLOAD_EVIDENCE_BUNDLE` |
| Report rebuild | `REBUILD_REPORT` |
| Retention lock | `MANAGE_RETENTION` |
| Root Cause review | `REVIEW_ROOT_CAUSE` |

## 5. Authorized Event Gateway

Module:

```text
backend/app/integrations/feishu/authorized_events.py
```

When `FEISHU_IDENTITY_RBAC_ENABLED=true`:

```text
Feishu event
  -> tenant_key + open_id
  -> Identity resolution
  -> G1 Case resolution
  -> deterministic Intent / card action
  -> Capability
  -> Case ACL
  -> ALLOW ? existing G1 dispatch_event : deterministic denial
```

Business services are called **only after ALLOW**.

Denied message events receive deterministic user feedback and a dedicated denial-idempotency record. Denied card actions return an error toast and never enter the existing control handler.

When the feature flag is disabled, the gateway delegates directly to existing G1 dispatch for development/backward compatibility.

## 6. Transport parity

Both transports route through the same authorized gateway:

- HTTP callback: `backend/app/api/v1/feishu_callback.py`
- official-SDK WebSocket long connection: `backend/app/integrations/feishu/long_connection.py`

The WebSocket card adapter now preserves `header.tenant_key`; card authorization therefore uses the same `tenant_key + open_id` identity key as messages and HTTP callbacks.

HTTP callbacks explicitly commit the request-scoped Session after successful authorized dispatch so Identity discovery, authorization audit/idempotency and normal event state are persisted.

## 7. Production fail-closed flag

Config:

```text
FEISHU_IDENTITY_RBAC_ENABLED
FEISHU_IDENTITY_DISCOVER_UNMAPPED
```

Development default keeps G1 legacy callback behavior available.

Production startup invariant:

```text
APP_ENV=production
+ FEISHU_LIVE_ENABLED=true
=> FEISHU_IDENTITY_RBAC_ENABLED must be true
```

Otherwise startup fails with:

```text
PRODUCTION_FEISHU_RBAC_REQUIRED
```

## 8. Management API

Router:

```text
backend/app/api/v1/feishu_governance.py
```

Admin/Service-only management endpoints:

```text
GET   /api/v1/feishu/identities
POST  /api/v1/feishu/identities
PATCH /api/v1/feishu/identities/{identity_id}

GET   /api/v1/cases/{case_id}/acl
PUT   /api/v1/cases/{case_id}/acl

GET   /api/v1/feishu/cases/bindings
POST  /api/v1/feishu/cases/bindings
POST  /api/v1/feishu/cases/{case_id}/close-binding
```

Write APIs use the existing idempotency service and audit material changes.

## 9. Migration

`0022_feishu_identity_rbac_v1` creates:

```text
feishu_user_identities
case_acl_entries
```

with tenant/open identity uniqueness, Case ACL uniqueness, indexes and `ALLOW/DENY` constraint.

## 10. Focused verification

- `test_feishu_identity_rbac_v1.py`
  - unknown user -> PENDING, no implicit privilege
  - tenant isolation
  - disabled fail-closed
  - Viewer vs Engineer capability matrix

- `test_feishu_case_acl_v1.py`
  - explicit DENY overrides Engineer role
  - ALLOW does not elevate Viewer
  - expired ACL no longer blocks
  - Case Owner does not elevate authority

- `test_feishu_authorized_events_v1.py`
  - unknown user cannot enqueue diagnosis
  - Viewer cannot enqueue STOP_REPRODUCTION
  - Engineer can control after authorization
  - Viewer card STOP is denied before handler
  - WebSocket card carries tenant_key

- `test_feishu_governance_api_v1.py`
  - identity upsert/patch idempotency
  - Case ACL desired-state replacement/idempotency

Full repository CI remains authoritative before Ready.

## 11. Explicit non-goals

G2 does not implement:

- G3 Feishu document ACL synchronization;
- AI1 Semantic Router;
- AI2 Diagnostic Loop;
- AI3 Case Copilot;
- AI-generated authorization decisions.

Authorization remains deterministic and cannot be overridden by AI.

# Feishu Case Gateway G1 V1 Implementation Trace

> Scope: **G1 / One Group One Active Case**  
> Baseline: `VOIP_AI_Intelligence_Layer_V1.0_方案与详细设计_编码实施版.md`  
> Branch: `agent/feishu-one-active-case-v1`  
> Status: **IMPLEMENTED / CI VALIDATION**

## 1. Frozen business invariant

A real Feishu source group is identified by:

```text
tenant_key + chat_id
```

For that business key, at most one Case may be `ACTIVE` at a time.

Legacy/default card-delivery bindings without `tenant_key` are intentionally outside this G1 invariant and keep their previous conservative correlation behavior.

## 2. Resolver priority

`backend/app/integrations/feishu/case_resolver.py`

1. Explicit Case reference.
2. Reply / thread anchor.
3. Tenant-bound chat Active Case.
4. Device + specific symptom + 24-hour same-chat fingerprint.
5. Fail closed / disambiguate.

Safety rules:

- An invalid explicit Case reference never falls back to another Case.
- Historical thread replies may resolve a historical Case even after the group later hosts a new Active Case.
- Multiple fingerprint candidates are never guessed.
- Empty-tenant legacy events do not opt into Active-Case routing.

## 3. Binding persistence contract

Migration:

```text
backend/migrations/versions/0021_feishu_case_governance_v1.py
```

Adds lifecycle fields to `feishu_case_bindings`:

- `binding_state`
- `binding_generation`
- `activated_at`
- `closed_at`
- `created_by_open_id`
- `close_reason`

PostgreSQL final consistency guard:

```text
uq_feishu_active_case_per_chat
(source_tenant_key, receive_id)
WHERE binding_state = 'ACTIVE'
  AND receive_id_type = 'chat_id'
  AND source_tenant_key IS NOT NULL
  AND source_tenant_key <> ''
```

Migration reconciliation:

- terminal Case bindings are closed;
- for pre-existing duplicate tenant-bound Active bindings, only the newest remains Active;
- older rows are retained as history and marked `MIGRATION_SUPERSEDED`;
- `binding_generation` preserves reuse history of a chat.

## 4. Runtime binding contract

`backend/app/integrations/feishu/service.py`

`bind_case_to_chat()` now guarantees:

- same Case is not silently moved to another source chat;
- application-level Active Case precheck before insertion;
- database partial unique index is the final concurrent-race guard;
- a losing bind raises `FeishuActiveCaseConflict`;
- a swallowed conflict marks only the current transaction rollback-only, so a caller cannot commit an orphan loser Case;
- low-level binding code does not directly roll back an enclosing idempotent/message transaction.

## 5. Feishu message behavior

`backend/app/integrations/feishu/events.py`

For a tenant-bound group with an Active Case:

### Normal diagnosis-style message

```text
@VOIP AI 这个设备又有电流音，帮忙继续分析
```

is normalized to:

```text
CASE_FOLLOW_UP
```

and is persisted against the current Active Case.

### Explicit independent new fault

```text
@VOIP AI 这是新的故障，另外一台设备也有电流音
```

returns:

```text
handled = active_case_conflict
missing_user_inputs = [new_group_or_admin_rebind]
```

and tells the user to create a new fault group. Admin rebind is intentionally deferred to G2.

### Attachments

When the resolver finds a Case, its `case_id` is copied to `source_context.correlated_case_id`; the existing attachment workflow therefore stores PCAP/audio/image/file Evidence into the same Case instead of creating a second Case.

## 6. Transaction and idempotency safety

Existing Feishu message idempotency remains the outer message contract.

A schema-introspection regression found during CI was fixed by inspecting the Session's current SQLAlchemy Connection rather than opening an Engine-level connection. This is required for SQLite `StaticPool` tests, where a second connection may reuse the same DBAPI connection and roll back uncommitted message/idempotency state.

Active Case bind conflict handling uses a rollback-only Session marker:

```text
conflict
  -> FeishuActiveCaseConflict
  -> transaction marked rollback-only
  -> any swallowed-conflict commit is rejected
  -> transaction owner performs rollback
```

This prevents orphan loser Cases without allowing a low-level service to decide the outer transaction rollback scope.

## 7. Verification

Focused tests:

- `backend/tests/test_feishu_case_resolver_v1.py`
  - Active Case default context
  - explicit Case priority
  - invalid explicit Case fail-closed
  - historical thread priority
  - tenant isolation

- `backend/tests/test_feishu_one_active_case_v1.py`
  - second Active Case conflict
  - transaction-owner rollback
  - group reuse after terminal Case
  - same Case cross-chat rebind refusal
  - cross-tenant isolation
  - diagnosis-style message -> follow-up
  - explicit new fault -> fail closed
  - migration contract

- `backend/tests/test_feishu_conflict_rollback_guard_v1.py`
  - swallowed conflict cannot commit an orphan loser Case

Repository-wide CI remains authoritative for regression acceptance:

- Python compile
- AI Contract / AI E1-E6
- M7 contract
- clean PostgreSQL Alembic upgrade through `0021`
- full backend regression
- Preliminary Evidence Report software release gate
- frontend dependency audit + production build

## 8. Explicit non-goals of PR-A

This PR does **not** implement:

- G2 Feishu Identity / RBAC;
- G3 Feishu Document ACL synchronization;
- AI1 Semantic Router;
- AI2 Diagnostic Loop;
- AI3 Case Copilot;
- Admin group rebind UX/API.

These remain separate implementation units so G1 can be independently reviewed and rolled back.

# VOIP AI Intelligence V1 — Static Acceptance Record

Date: 2026-08-19

## 1. Acceptance contract

Software Release Gate may not use implementation gaps as external Pending. Only the following production/environment categories may remain after software PASS:

1. `LIVE_FEISHU_TENANT`
2. `REAL_DUT_END_TO_END`
3. `REAL_SEMANTIC_AND_GOLDEN_DATASET`

`CONTROLLED_PLANNER` remains disabled until the frozen promotion/Policy/Registry/real-DUT gates are satisfied.

## 2. Stack under acceptance

- PR #5 — G1 one-group-one-active-case
- PR #6 — G2 Feishu Identity/RBAC
- PR #7 — G3 Feishu document ACL sync
- PR #8 — AI1 Semantic Router SHADOW
- PR #9 — AI3 Case Copilot
- PR #10 — AI2 Diagnostic Loop SHADOW/SUGGEST

G1/G2/G3 retain their previously machine-validated software results because this acceptance pass did not modify those branches.

AI1/AI3/AI2 latest heads require fresh machine validation because static acceptance produced new software commits.

## 3. Historical machine evidence

Historical software PASS exists for:

- G1: compile / AI Contract / AI E1-E6 / M7 / migration 0021 / 464 backend tests / Evidence Report / frontend build.
- G2: software gate including migration 0022 and security/RBAC regression.
- G3: migration 0023 / 489 backend tests / Evidence Report / frontend build.
- AI1 historical head `74d9c18b6f761efaa6747b77a62c223074773d63`: workflow #324, 501 backend tests and dedicated AI1 gate PASS.

The historical AI1 PASS is **not** accepted as evidence for the latest AI1 head because tenant-isolation code was changed after that run.

## 4. Static acceptance defects found and fixed

The acceptance pass found actual software defects and fixed them rather than classifying them as environment Pending.

### AI1

1. **Cross-Tenant semantic replay**
   - Previous semantic idempotency persisted/queried globally unique raw `message_id` before tenant isolation.
   - Fix: persisted semantic delivery key is now `sem:SHA256(tenant_key + message_id)` for live Feishu traffic and Case-scoped SHA-256 for tenant-less Admin/debug traffic.
   - Added same-message-id/two-tenant regression coverage.

### AI3

2. **API cross-authorization idempotent replay**
   - A request key could replay an Engineer-projected answer to another actor/role.
   - Fix: API key scoped to Case + actor + role + request ID; Service re-validates Case/actor/role/question hash before replay.

3. **Feishu cross-Tenant Copilot replay**
   - Generic idempotency could replay by delivery ID before Service authorization-context validation.
   - Fix: Feishu Copilot key scoped to tenant + Case + actor + role + delivery ID.

### AI2

4. **Concurrent suggestion acceptance race**
   - Two card callbacks could both observe PROPOSED and create duplicate deterministic workflows.
   - Fix: Cycle acceptance serialized with database row locks and explicit state contract.

5. **Reproduction broker failure could lose task**
   - Cycle could appear DISPATCHED before broker publication was durably confirmed.
   - Fix: recoverable two-phase `PROPOSED -> ACCEPTED(Session committed) -> broker publish -> DISPATCHED` handoff.

6. **Commit-to-broker duplicate-publish window**
   - Concurrent callback during broker confirmation could republish the same Session.
   - Fix: persisted 60-second publish lease, same-Session retry, and Session-state reconciliation.

7. **Recoverable state had no Feishu retry UX**
   - ACCEPTED reproduction state could be recoverable in backend but not visible/actionable on the Case Card.
   - Fix: `重试 AI2 任务投递` card state; DISPATCHED hides accept/retry controls.

8. **Cycle allocation race**
   - Automatic sidecar and manual `/cycles/next` could race `cycle_no`/fingerprint uniqueness.
   - Fix: Case-row serialization before Cycle allocation/idempotency check.

9. **Formal DiagnosisDecision pollution**
   - AI2 metadata was copied into `decision.summary`, which is later persisted in formal `DiagnosisRun`.
   - Fix: AI2 sidecar never mutates the formal Decision; cycle metadata remains in `AIDiagnosticCycle`/Audit/API/Card only.

10. **Safety observability masking**
    - Cycle API returned hard-coded false authority flags rather than actual persisted values.
    - Fix: return persisted values; metrics count violations; DB check constraints reject any V1 row with AI formal-result/dispatch authority flags true.

11. **Short VOIP caller/callee privacy gap**
    - Generic phone regex could miss short SIP extensions.
    - Fix: caller/callee fields are structurally redacted regardless of number length.

## 5. Hard authority constraints

For AI2 V1 the database itself enforces:

- `runtime_stage IN ('SHADOW','SUGGEST')`
- `formal_result_changed = false`
- `dispatch_attempted = false`
- `dispatch_allowed = false`

Application-level contracts additionally reject CONTROLLED_PLANNER in the AI2 V1 cycle service.

AI1/AI3/AI2 cannot confirm Root Cause, elevate Evidence Level, execute raw SSH/shell/AIM commands, or bypass G1/G2/Registry/Policy/Orchestrator authority.

## 6. Privacy acceptance

Reasoning Gateway receives only compact/redacted structured context:

- no raw PCAP/PCM/WAV upload
- credentials/device_info excluded
- DUT identifiers excluded by default
- Evidence metadata allow-listed
- nested password/token/secret/cookie/authorization fields redacted
- IP/MAC/phone identifiers redacted
- short SIP caller/callee identifiers structurally redacted
- untrusted prompt-injection-like evidence text is not treated as model instruction

AI3 Viewer projection excludes RAW Evidence before gateway generation.

## 7. Idempotency and concurrency acceptance

- AI1: tenant-scoped semantic delivery key.
- AI3 API: Case+actor+role+request-scoped key + Service validation.
- AI3 Feishu: tenant+Case+actor+role+delivery-scoped key.
- AI2 Cycle creation: serialized per Case.
- AI2 SUGGEST acceptance: serialized per Cycle.
- AI2 Reproduction: same persisted Session reused on recoverable publish retry.

## 8. Feishu authorization acceptance

Both HTTP callback and WebSocket/SDK entry paths use the same `dispatch_authorized_event()` gateway.

AI2 suggestion acceptance performs two authorization layers:

1. `RUN_AI_SUGGESTION`
2. underlying capability:
   - Question / user evidence -> `ADD_EVIDENCE`
   - Reproduction -> `CONTROL_REPRODUCTION`
   - Experiment -> `RUN_REGISTERED_EXPERIMENT`

Case ACL `DENY` remains authoritative. AI does not participate in RBAC decisions.

## 9. Dedicated latest-head regression coverage

AI1 extended test coverage includes multi-tenant semantic replay isolation.

AI3 dedicated gate includes:

- API actor/role/question idempotency isolation
- Feishu tenant-scoped idempotency isolation
- role-aware Evidence projection
- Grounding and Root Cause fail-closed
- control-intent isolation
- SAVEPOINT runtime failure isolation

AI2 dedicated gate includes:

- SHADOW/SUGGEST cycle contract
- API/metrics and DB hard-zero authority checks
- formal DiagnosisDecision immutability
- Cycle concurrency
- Reasoning Gateway redaction
- registered Suggest bridge
- suggestion acceptance concurrency
- Reproduction two-phase publish/recovery/lease
- Feishu SUGGEST/RBAC
- Feishu recoverable retry card
- post-commit dispatch ordering

## 10. Stack integrity

After AI1 hardening, changes are propagated forward in order:

AI1 PR #8 -> AI3 PR #9 -> AI2 PR #10.

Acceptance requires:

- PR #9 contains latest PR #8 head and is `behind_by=0`.
- PR #10 contains latest PR #9 head and is `behind_by=0`.
- PR #10 relative diff contains only AI2-specific additions/overrides.

## 11. Current machine-validation blocker

GitHub-hosted Actions currently fails before any workflow step is created:

- `steps=null`
- `logs_url=null`
- failure before checkout/setup/compile/pytest/migration/frontend build

This behavior was reproduced on Ubuntu/Windows/macOS minimal runner probes and latest PR reruns.

Therefore latest-head software PASS is **not claimed** for AI1/AI3/AI2. This document records static acceptance and fixes; it does not substitute for executable CI.

## 12. Mandatory machine Gate after runner recovery

Latest heads must execute and PASS:

1. Python compile
2. AI Contract Coverage Gate
3. AI E1-E6 regression
4. AI1 Semantic Router gate
5. AI3 Case Copilot gate
6. AI2 SHADOW/SUGGEST gate
7. M7 acceptance contract
8. PostgreSQL clean migration through `0026_ai_diagnostic_loop_v1`
9. full backend regression
10. Preliminary Evidence Report software release gate
11. frontend dependency audit
12. production frontend build

Any actual failure discovered by these steps is a software defect to be fixed; it may not be converted into a fourth Pending.

## 13. External production gates after software PASS

Only the frozen external categories may remain:

- live Feishu tenant
- real DUT end-to-end
- real semantic/Golden Dataset validation

No software Release Gate PASS is asserted until Section 12 is fully green on latest heads.

# AI2 Diagnostic Loop V1 — Implementation Trace

## Scope

This change implements the frozen VOIP AI Intelligence Layer V1.0 AI2 software boundary for **SHADOW** and **SUGGEST**.

AI2 reuses the existing Case Intelligence Snapshot, deterministic diagnosis baseline, AI Proposal validator, contradiction critic, discriminating planner, registered Question/Reproduction/Experiment registries, Feishu G2 Identity/RBAC, and deterministic Reproduction/Experiment orchestrators. It does not create a second diagnosis or execution authority.

## Automatic runtime flow

`Deterministic Diagnosis`
→ persist current Hypothesis revisions
→ AI2 sidecar SAVEPOINT
→ `CaseIntelligenceSnapshot`
→ reuse existing accepted SHADOW proposal when possible, otherwise Reasoning Gateway
→ strict current-Case Evidence validation
→ contradiction critic
→ bounded stop decision
→ optional registered recommendation in SUGGEST
→ persisted `AIDiagnosticCycle`
→ audit / metrics

AI2 sidecar failure rolls back only the sidecar savepoint. The deterministic diagnosis transaction remains authoritative and continues.

**The sidecar never mutates the formal `DiagnosisDecision` object.** AI2 cycle IDs, recommendations and continuation state live only in `AIDiagnosticCycle`, Audit, the dedicated AI2 API and Feishu projection. They are not copied into `DiagnosisRun.decision_json` or `summary_json`.

## Stage behavior

### SHADOW

- records AI hypothesis / known / unknown / excluded / critic;
- AI hypotheses remain OPEN, L5 and non-confirmable under the existing AI Proposal contract;
- no cycle-level next action is exposed;
- `formal_result_changed = false`;
- `dispatch_attempted = false`;
- `dispatch_allowed = false`.

### SUGGEST

- includes SHADOW behavior;
- recommendation must resolve to an existing registered Question / Reproduction Profile / Experiment Profile;
- if the model has no usable registered recommendation, the deterministic discriminating planner may select an existing registered discriminator;
- actionable recommendations persist with `suggestion_state=PROPOSED`;
- the Case card displays the latest actionable suggestion and a **采纳 AI2 建议** button;
- the card explicitly states that the suggestion is not Root Cause and is not auto-executed by AI.

### Explicit user-confirmation bridge

Clicking **采纳 AI2 建议** is not AI execution.

The callback path is:

`Feishu card click`
→ G2 active Identity
→ `RUN_AI_SUGGESTION`
→ underlying capability re-check
→ latest SUGGEST Cycle check
→ persisted registered ID check
→ deterministic registry / orchestrator re-validation
→ database commit
→ asynchronous worker enqueue only after commit where needed.

Underlying capability checks are:

- Question / user evidence request → `ADD_EVIDENCE`;
- Reproduction Profile → `CONTROL_REPRODUCTION`;
- Experiment Profile → `RUN_REGISTERED_EXPERIMENT`.

A Case ACL `DENY` on the underlying capability still blocks the suggestion even if the user has `RUN_AI_SUGGESTION` globally.

Suggestion lifecycle is persisted as:

`NONE | PROPOSED | ACCEPTED | DISPATCHED | FAILED`

with `accepted_by`, `accepted_at`, `execution_ref_type`, `execution_ref_id`, and error metadata.

### Reproduction two-phase dispatch recovery

Reproduction recommendations use a recoverable handoff instead of declaring success before the broker accepts the task:

`PROPOSED`
→ explicit user confirmation
→ create deterministic `ReproductionSession`
→ persist `ACCEPTED`
→ commit
→ publish `reproduction.start(session_id)`
→ Celery `after_task_publish`
→ persist `DISPATCHED`.

If broker publication fails, the Cycle remains `ACCEPTED` and the same persisted Session can be retried; a second Session is not created.

Additional race protection:

- Cycle acceptance uses `SELECT ... FOR UPDATE`;
- a persisted 60-second publish lease suppresses simultaneous duplicate card callbacks during commit→broker confirmation;
- if the Session has already left `CREATED`, a later click reconciles the Cycle to `DISPATCHED` instead of publishing again;
- `after_task_publish` confirms broker acceptance and clears the publish-pending marker;
- Feishu Card keeps a **重试 AI2 任务投递** button only for recoverable `ACCEPTED` reproduction state;
- `DISPATCHED` hides both accept/retry buttons.

### CONTROLLED_PLANNER

Not enabled by this V1 software gate. Calling the AI2 cycle service with CONTROLLED_PLANNER fails closed with:

`AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE`

Existing machine-generated promotion attestation, Policy, registry and deterministic Orchestrator remain the only future path to controlled autonomous execution.

## Persistence

Migration: `0026_ai_diagnostic_loop_v1`

Table: `ai_diagnostic_cycles`

Important invariants:

- unique `(case_id, cycle_no)`;
- unique `(case_id, snapshot_fingerprint, runtime_stage)`;
- stage limited to `SHADOW | SUGGEST`;
- status limited to `COMPLETED | DEGRADED | STOPPED | REQUIRE_HUMAN`;
- persisted snapshot/evidence fingerprints, proposal link, critic, recommendation, stop reason and no-progress count;
- Case row is locked while allocating Cycle/idempotency state, so manual `/cycles/next` and automatic sidecar cannot race `cycle_no`/fingerprint uniqueness;
- DB check constraints enforce `formal_result_changed = false`;
- DB check constraints enforce `dispatch_attempted = false`;
- DB check constraints enforce `dispatch_allowed = false`.

The hard-zero authority rules therefore exist at both service and database level.

## Stop conditions

- Case already `ROOT_CAUSE_CONFIRMED / RESOLVED / CLOSED`;
- Evidence sufficient / complete;
- unchanged Evidence fingerprint reaches `diagnosis_no_progress_limit`;
- `diagnosis_max_cycles` reached;
- AI proposal unavailable or invalid;
- hard deterministic contradiction;
- no useful registered discriminator.

## Reasoning Gateway privacy boundary

AI2 may build an internal SERVICE-level engineering Snapshot, but the Reasoning Gateway receives only `compact_context()` plus a recursively redacted deterministic baseline.

- raw PCAP/PCM/WAV bytes are never uploaded;
- `device_info` and credential fields are not included in gateway device projection;
- device identifiers are excluded by default (`REASONING_GATEWAY_INCLUDE_DEVICE_IDENTIFIERS=false`);
- Evidence metadata is allow-listed;
- nested password/token/secret/cookie/authorization fields are recursively redacted;
- IP/MAC/phone identifiers are redacted;
- SIP caller/callee fields are structurally redacted even for 3–6 digit short numbers/extensions;
- prompt-injection-like evidence text is treated as untrusted data and redacted before transport.

## API and observability

- `GET /api/v1/cases/{case_id}/ai/cycles`
  - `CASE_READ + DIAGNOSIS_READ`;
  - includes suggestion lifecycle and deterministic execution references;
  - returns the actual persisted authority booleans instead of masking them.
- `POST /api/v1/cases/{case_id}/ai/cycles/next`
  - `CASE_READ + DIAGNOSIS_RUN`;
  - one bounded cycle; no direct device action.
- `GET /api/v1/ai/diagnostic-loop/metrics`
  - cycle count by stage/status/continue decision/stop reason/suggestion state;
  - accepted suggestion count;
  - hard-zero counters for AI formal-result changes, AI dispatch attempts and AI dispatch-authority rows.

## Feishu / AI3 inherited isolation

PR #10 is stacked on AI3 and includes the AI3 acceptance hardening:

- API Copilot idempotency key is scoped to Case + actor + role + request ID using a bounded SHA-256 key;
- Service replay validates Case, actor, role and question hash;
- Feishu Copilot idempotency is scoped to `tenant + case + actor + role + delivery_id` before the idempotency layer can replay a response;
- identical Feishu message IDs in different tenants cannot cross-replay a Copilot result.

AI2 card actions continue through the same G2 Authorized Event Gateway and additionally re-check the underlying Question/Reproduction/Experiment capability.

## Feature flags

- `AI_DIAGNOSTIC_LOOP_ENABLED=false` by default;
- existing `AI_PROMOTION_STAGE` selects OFF / SHADOW / SUGGEST / CONTROLLED_PLANNER;
- this V1 implementation accepts SHADOW/SUGGEST only;
- `AI_DIAGNOSTIC_LOOP_WORKFLOW_VERSION=ai-diagnostic-loop-v1`.

## Dedicated software gate

CI includes:

```bash
PYTHONPATH=backend:. pytest -q \
  backend/tests/test_ai2_diagnostic_loop_v1.py \
  backend/tests/test_ai2_cycles_api_v1.py \
  backend/tests/test_ai2_diagnosis_sidecar_v1.py \
  backend/tests/test_ai2_cycle_concurrency_contract_v1.py \
  backend/tests/test_ai2_reasoning_gateway_redaction_v1.py \
  backend/tests/test_ai2_suggest_bridge_v1.py \
  backend/tests/test_ai2_suggest_concurrency_contract_v1.py \
  backend/tests/test_ai2_reproduction_publish_recovery_v1.py \
  backend/tests/test_ai2_feishu_suggest_v1.py \
  backend/tests/test_ai2_feishu_retry_card_v1.py \
  backend/tests/test_ai2_feishu_dispatch_order_v1.py
```

The inherited AI3 Gate also includes:

```bash
backend/tests/test_ai3_copilot_idempotency_isolation_v1.py
backend/tests/test_ai3_feishu_tenant_idempotency_v1.py
```

Focused coverage includes:

- SHADOW no planning/dispatch/formal result mutation;
- exact formal `DiagnosisDecision` immutability on sidecar success and failure;
- SUGGEST registered recommendation with `PROPOSED` lifecycle;
- Snapshot+Stage idempotency and proposal reuse;
- per-Case Cycle serialization;
- no-progress, Evidence-sufficient, Root Cause terminal and max-cycle stops;
- CONTROLLED_PLANNER fail-closed;
- unregistered recommendation and raw-command marker fail-closed;
- deterministic sidecar automatic invocation and failure isolation;
- explicit user confirmation required;
- stale suggestion rejected;
- sequential and concurrent duplicate click protection;
- recoverable two-phase Reproduction dispatch;
- publish lease, broker confirmation and Session-state reconciliation;
- Viewer denied;
- Engineer still constrained by Case ACL on underlying action;
- G2 RBAC disabled → suggestion acceptance fail-closed;
- HTTP and Feishu WebSocket paths both use the Authorized Event Gateway;
- Case card contains no raw command authority and exposes retry state without leaking Session IDs;
- Reasoning Gateway credential/identifier redaction including short VOIP numbers;
- API does not mask persisted safety invariant values;
- database rejects any true AI authority flag in V1 SHADOW/SUGGEST rows.

## Static acceptance defects found and fixed

During pre-CI acceptance, the following real software defects were found and fixed rather than being deferred as environment Pending:

1. **AI3 API idempotent replay authorization isolation** — existing request keys could replay an answer produced for another actor/role; fixed with scoped hashed key + Service validation.
2. **AI3 Feishu cross-Tenant replay isolation** — message/event ID alone could collide before Service validation; fixed with tenant/Case/actor/role scoped hashed idempotency.
3. **AI2 concurrent suggestion acceptance** — two card callbacks could both create deterministic workflows; fixed with row locking and state contract.
4. **AI2 Reproduction broker failure** — Cycle could be marked DISPATCHED before broker publish and permanently lose the task; fixed with two-phase ACCEPTED→DISPATCHED handoff.
5. **AI2 Reproduction duplicate-publish window** — commit→broker confirmation allowed a short concurrent republish race; fixed with a 60-second persisted publish lease and Session-state reconciliation.
6. **AI2 Feishu retry UX** — recoverable ACCEPTED state originally had no retry button; fixed with explicit retry projection.
7. **AI2 Cycle allocation race** — concurrent automatic/manual cycles could collide on cycle number/fingerprint; fixed with Case row serialization.
8. **AI2 formal-result pollution** — successful sidecar metadata was copied into `decision.summary`, which is later persisted as formal `DiagnosisRun`; removed completely.
9. **AI2 observability masking** — Cycle API hardcoded authority fields false instead of exposing stored values; fixed and DB hard-zero constraints added.
10. **Reasoning Gateway short VOIP number privacy** — short caller/callee extensions were below the generic phone-regex threshold; fixed with structural party-field redaction.

## Authority invariants

AI2 must never:

- write/replace or mutate a formal `DiagnosisDecision`;
- promote AI hypothesis beyond OPEN/L5/non-confirmable;
- confirm Root Cause or elevate Evidence Level;
- generate/execute raw shell, SSH or AIM commands;
- bypass Identity/RBAC, Case ACL, Registry, Policy or Orchestrator;
- execute an unregistered model-invented action;
- treat SUGGEST as implicit user approval.

Formal diagnosis remains deterministic. Root Cause authority remains deterministic causal confirmation / authorized human review / fix verification.

## Current validation status

Implementation remains on `agent/ai2-diagnostic-loop-v1`, Draft PR #10.

GitHub-hosted Actions currently fails before any workflow step is created (`steps=null`, `logs_url=null`) across the AI3/AI2 branches and even minimal runner probes on Ubuntu/Windows/macOS. Therefore the latest static-hardening commits have **not yet received executable CI evidence**, and **software PASS is not claimed**.

PR #10 remains Draft until GitHub Actions can actually execute and all of the following PASS on the latest head:

- Python compile;
- AI Contract Coverage Gate;
- AI E1–E6 regression;
- AI1 Semantic Router gate;
- AI3 Case Copilot gate including actor/role/question and tenant idempotency isolation;
- AI2 SHADOW/SUGGEST dedicated gate listed above;
- M7 acceptance contract;
- PostgreSQL clean Alembic upgrade through `0026_ai_diagnostic_loop_v1`;
- full backend regression;
- Preliminary Evidence Report software release gate;
- frontend dependency audit and production build.

External production gates remain separate and are not used to excuse software failures: live Feishu tenant, real DUT end-to-end, and real semantic/Golden Dataset validation.

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

with `accepted_by`, `accepted_at`, `execution_ref_type`, `execution_ref_id`, and error metadata. Duplicate callback after `DISPATCHED` is idempotent and does not create a second workflow object.

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
- explicit AI authority fields remain false even after a user-confirmed deterministic workflow is created.

## Stop conditions

- Case already `ROOT_CAUSE_CONFIRMED / RESOLVED / CLOSED`;
- Evidence sufficient / complete;
- unchanged Evidence fingerprint reaches `diagnosis_no_progress_limit`;
- `diagnosis_max_cycles` reached;
- AI proposal unavailable or invalid;
- hard deterministic contradiction;
- no useful registered discriminator.

## API and observability

- `GET /api/v1/cases/{case_id}/ai/cycles`
  - `CASE_READ + DIAGNOSIS_READ`;
  - includes suggestion lifecycle and deterministic execution references.
- `POST /api/v1/cases/{case_id}/ai/cycles/next`
  - `CASE_READ + DIAGNOSIS_RUN`;
  - one bounded cycle; no direct device action.
- `GET /api/v1/ai/diagnostic-loop/metrics`
  - cycle count by stage/status/continue decision/stop reason/suggestion state;
  - accepted suggestion count;
  - exposes hard-zero counters for AI formal-result changes and AI dispatch attempts.

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
  backend/tests/test_ai2_suggest_bridge_v1.py \
  backend/tests/test_ai2_feishu_suggest_v1.py
```

Focused coverage includes:

- SHADOW no planning/dispatch/formal result mutation;
- SUGGEST registered recommendation with `PROPOSED` lifecycle;
- Snapshot+Stage idempotency and proposal reuse;
- no-progress, Evidence-sufficient, Root Cause terminal and max-cycle stops;
- CONTROLLED_PLANNER fail-closed;
- unregistered recommendation and raw-command marker fail-closed;
- deterministic sidecar automatic invocation and failure isolation;
- explicit user confirmation required;
- stale suggestion rejected;
- duplicate click idempotent;
- Viewer denied;
- Engineer still constrained by Case ACL on underlying action;
- G2 RBAC disabled → suggestion acceptance fail-closed;
- Case card contains no raw command authority;
- metrics hard-zero observability.

## Authority invariants

AI2 must never:

- write/replace a formal `DiagnosisDecision`;
- promote AI hypothesis beyond OPEN/L5/non-confirmable;
- confirm Root Cause or elevate Evidence Level;
- generate/execute raw shell, SSH or AIM commands;
- bypass Identity/RBAC, Case ACL, Registry, Policy or Orchestrator;
- execute an unregistered model-invented action;
- treat SUGGEST as implicit user approval.

Formal diagnosis remains deterministic. Root Cause authority remains deterministic causal confirmation / authorized human review / fix verification.

## Current validation status

Implementation is committed on `agent/ai2-diagnostic-loop-v1`, Draft PR #10.

GitHub Actions run `32168965959` / run #429 failed before any workflow step was created: `ai-contracts` returned `steps=null` and `logs_url=null`. The same pre-step failure also occurred on the AI3 branch and earlier AI2 runs. Therefore no pytest, migration, backend regression, Evidence Report gate, or frontend-build failure has been observed from those runs, but **software PASS is not claimed**.

PR #10 remains Draft until GitHub Actions can actually execute and the dedicated AI2 gate, migration through `0026_ai_diagnostic_loop_v1`, full backend regression, Evidence Report release gate and frontend production build all PASS.

External production gates remain separate: live Reasoning Gateway/real Case behavior, real Golden/Eval data, real Feishu tenant and real DUT end-to-end validation.

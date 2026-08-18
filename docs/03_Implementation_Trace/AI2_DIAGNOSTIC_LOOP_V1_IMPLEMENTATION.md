# AI2 Diagnostic Loop V1 — Implementation Trace

## Scope

This change implements the frozen VOIP AI Intelligence Layer V1.0 AI2 software boundary for **SHADOW** and **SUGGEST** only.

AI2 reuses the existing Case Intelligence Snapshot, deterministic diagnosis baseline, AI Proposal validator, contradiction critic, discriminating planner, registered Question/Reproduction/Experiment registries, and AI promotion runtime. It does not create a second diagnosis authority.

## Runtime flow

`CaseIntelligenceSnapshot`
→ deterministic baseline
→ AI hypothesis proposal
→ strict schema/current-Case Evidence validation
→ contradiction critic
→ optional registered recommendation in SUGGEST
→ persisted `AIDiagnosticCycle`
→ audit

V1 deliberately stops before Policy/Orchestrator dispatch.

## Stage behavior

### SHADOW

- model may generate hypotheses/critique/explanation under the existing `AIProposal` contract;
- cycle persists `known / unknown / excluded / hypotheses / critic`;
- no `next_action` is exposed by the cycle;
- `formal_result_changed = false`;
- `dispatch_attempted = false`;
- `dispatch_allowed = false`.

### SUGGEST

- includes SHADOW behavior;
- an AI recommendation must resolve to an existing registered Question/Reproduction/Experiment identifier;
- if the model gives no registered recommendation, the deterministic discriminating planner may select an existing registered question/profile;
- suggestion remains non-executing;
- `dispatch_attempted = false` and `dispatch_allowed = false` are immutable V1 behavior.

### CONTROLLED_PLANNER

Not enabled by this V1 software gate. Calling the AI2 cycle service with CONTROLLED_PLANNER fails closed with `AI2_CONTROLLED_PLANNER_NOT_ENABLED_BY_V1_GATE`.

Existing machine-generated promotion attestation, RBAC, Policy, registry and deterministic Orchestrator remain the only future path to execution.

## Persistence

Migration: `0026_ai_diagnostic_loop_v1`

Table: `ai_diagnostic_cycles`

Important invariants:

- unique `(case_id, cycle_no)`;
- unique `(case_id, snapshot_fingerprint, runtime_stage)` for idempotency;
- runtime stage limited to `SHADOW | SUGGEST`;
- cycle status limited to `COMPLETED | DEGRADED | STOPPED | REQUIRE_HUMAN`;
- every row records snapshot/evidence fingerprints, proposal link, critic, registered recommendation, stop reason and no-progress counter;
- every row explicitly records `formal_result_changed=false`, `dispatch_attempted=false`, `dispatch_allowed=false`.

## Stop conditions

Implemented in the cycle service:

- current Case already `ROOT_CAUSE_CONFIRMED / RESOLVED / CLOSED` → STOP;
- reproduction Evidence sufficiency is sufficient/complete → STOP;
- unchanged Evidence fingerprint reaches configured `diagnosis_no_progress_limit` → STOP;
- configured `diagnosis_max_cycles` reached → STOP;
- AI proposal unavailable/invalid → REQUIRE_HUMAN / degraded;
- hard deterministic contradiction → REQUIRE_HUMAN;
- no useful registered discriminator in SUGGEST → REQUIRE_HUMAN.

## API

- `GET /api/v1/cases/{case_id}/ai/cycles`
  - requires `CASE_READ + DIAGNOSIS_READ`;
  - returns persisted cycles and immutable authority boundary fields.
- `POST /api/v1/cases/{case_id}/ai/cycles/next`
  - requires `CASE_READ + DIAGNOSIS_RUN`;
  - triggers one bounded cognitive cycle;
  - does not dispatch any device/reproduction/experiment action.

## Feature flags

- `AI_DIAGNOSTIC_LOOP_ENABLED=false` by default;
- existing `AI_PROMOTION_STAGE` selects OFF/SHADOW/SUGGEST/CONTROLLED_PLANNER;
- AI2 V1 accepts SHADOW/SUGGEST only;
- `AI_DIAGNOSTIC_LOOP_WORKFLOW_VERSION=ai-diagnostic-loop-v1`.

## Dedicated software gate

CI includes:

```bash
PYTHONPATH=backend:. pytest -q \
  backend/tests/test_ai2_diagnostic_loop_v1.py \
  backend/tests/test_ai2_cycles_api_v1.py
```

Focused contract coverage includes:

- SHADOW hypothesis/critic persistence without planning/dispatch;
- SUGGEST registered recommendation without dispatch authority;
- same Snapshot+Stage idempotency;
- no-progress stop;
- Evidence-sufficient stop;
- formally confirmed Root Cause stop;
- max-cycle stop;
- CONTROLLED_PLANNER fail-closed;
- unregistered model recommendation fail-closed;
- API authority contract.

## Authority invariants

AI2 must never:

- write a formal `DiagnosisDecision`;
- promote an AI hypothesis beyond OPEN/L5/non-confirmable;
- confirm Root Cause;
- elevate Evidence Level;
- generate/execute raw shell, SSH or AIM commands;
- bypass RBAC/Policy;
- dispatch an unregistered action;
- use SUGGEST as implicit approval for execution.

Formal diagnosis remains deterministic. Root Cause authority remains deterministic causal confirmation / authorized human review / fix verification.

## Software acceptance status

Implementation is present on `agent/ai2-diagnostic-loop-v1`.

Final PASS requires the repository workflow to execute the dedicated AI2 gate, migration through `0026_ai_diagnostic_loop_v1`, full backend regression, Evidence Report release gate and frontend production build. Those results must be recorded in the PR before it is marked Ready.

External production gates remain separate: live Reasoning Gateway/real Case behavior, real Golden/Eval data, and real DUT end-to-end validation.

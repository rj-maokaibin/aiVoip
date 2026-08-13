# Phase C3 — DiagnosticQuestion DAG, Diagnostic Experiments and Fix Verification

Status: **IMPLEMENTED / MOCK-PLATFORM VALIDATED**  
Scope: M6.2 Reproduction Intelligence C3  
Contract boundary: **EC-02 RESERVED / no real DUT command mapping**

## 1. Goal

Phase C3 closes the deterministic diagnosis loop above the C1/C2 autonomous reproduction and evidence pipeline:

```text
DiagnosticQuestion DAG
  -> approved ExperimentProfile
  -> baseline/variant reproduction
  -> EnvironmentComparator
  -> A/B or A-B-A causal assessment
  -> ROOT_CAUSE_CONFIRMED gate
  -> FixAction
  -> Fix Verification
  -> RESOLVED only after FIX_VERIFIED
```

C3 does not let the LLM confirm causality, invent an experiment, or execute an unregistered physical/DUT action. The deterministic question, experiment, environment, causal, and fix-verification services own the state transitions and evidence gates.

## 2. DiagnosticQuestion DAG

`profiles/questions/voip_v1.yaml` defines the frozen V1 question registry. The registry validates IDs, child references, experiment references, evidence requirements, levels, information gain and cycle-free DAG structure.

Implemented levels:

- SYMPTOM_CONFIRM
- FAULT_DOMAIN
- FAULT_LAYER
- ROOT_CAUSE
- FIX_VERIFICATION

`DiagnosticQuestionGraph` persists question instances, selects the next approved open question by deterministic priority/information-gain metadata, and enforces required evidence before a question may be answered.

Important evidence rule: a caller cannot answer a question by merely submitting a textual conclusion. Required `must_findings` and minimum evidence level are verified against current evidence/causal objects. Unknown evidence references do not satisfy the gate.

## 3. ExperimentProfile registry

`profiles/experiments/voip_v1.yaml` contains six approved single-variable experiment profiles:

1. `PHONE_SWAP_AB`
2. `LINE_SWAP_AB`
3. `FXS_PORT_SWAP_AB`
4. `POWER_SUPPLY_AB`
5. `DEVICE_SWAP_AB`
6. `POST_REBOOT_FIRST_CALL`

Each profile freezes:

- target finding
- applicable hypothesis codes
- reproduction profile
- independent variable
- expected-change paths
- must-equal controlled variables
- soft-drift paths
- confirmation policy
- experiment sequence
- external action instructions

Physical experiments never contain shell/AIM/reboot commands. The profile gate recursively rejects executable command/action keys.

`POST_REBOOT_FIRST_CALL` is explicitly an external L3 action. C3 only coordinates evidence collection before/after the externally performed reboot; it does **not** execute a real reboot command.

## 4. Environment snapshots and comparator

Every experiment reproduction run freezes a PRE environment snapshot after ARM and before the call, and a POST snapshot after the analyzed call. The snapshot is built from persisted/runtime/external inputs rather than by issuing unapproved DUT commands.

`EnvironmentComparator` classifies differences as:

- EXPECTED_CHANGE
- SOFT_DRIFT
- HARD_DRIFT

Overall comparison states:

- COMPARABLE
- COMPARABLE_WITH_SOFT_DRIFT
- NOT_COMPARABLE

Rules:

- the declared independent variable must actually change for a B variant;
- A2 in an A-B-A experiment must revert the independent variable to the A1 value;
- missing/changed must-equal controlled variables are HARD_DRIFT;
- HARD_DRIFT makes that run invalid for causal confirmation;
- a later clean retry of the same variant supersedes the invalid attempt for the active causal gate, while the earlier invalid comparison remains immutable audit evidence.

## 5. A/B, A-B-A and causal confirmation

`CausalConfirmationEngine` is deterministic and does not use a confidence threshold as a confirmation gate.

Supported confirmation policies:

- `AB_SUFFICIENT`
- `ABA_PREFERRED`
- `ABA_REQUIRED`
- `REPEAT_MATCH`
- `DIRECT_EVIDENCE`

Causal result states:

- HYPOTHESIS
- SUPPORTED
- STRONGLY_SUPPORTED
- ROOT_CAUSE_CONFIRMED
- CONTRADICTED

For the physical periodic-interference profiles, a single A/B generally reaches `STRONGLY_SUPPORTED`; A1 target MATCH -> B NO_MATCH -> A2 target MATCH with comparable environments can satisfy an `ABA_REQUIRED` root-cause confirmation gate.

Causal output is persisted as immutable L1 evidence and appends a new Hypothesis revision rather than overwriting history. Only a satisfied causal gate can transition the Case into `ROOT_CAUSE_CONFIRMED`.

## 6. Experiment orchestration

`DiagnosticExperimentOrchestrator` implements:

- experiment creation from an approved profile only;
- variant sequencing and run numbering;
- external-action completion tracking;
- Mock Platform reproduction session creation;
- PRE/POST environment snapshots;
- bounded one-call experiment completion;
- mandatory safe cleanup after the analyzed experiment call;
- invalid-variant retry;
- environment comparison;
- causal evaluation;
- root-question completion and FIX_VERIFICATION question creation.

A control B call is expected to be `NO_MATCH`. It is therefore not treated as a reproduction timeout. Once the experiment call is analyzed, C3 explicitly initiates the normal cleanup/finalization path and then evaluates the experiment result.

## 7. FixAction and Fix Verification

A FixAction can be created only when the Case is `ROOT_CAUSE_CONFIRMED` and the causal/hypothesis evidence is present. Creating the FixAction moves the Case to `RESOLVING`.

`FixVerificationService` compares verification calls against a confirmed baseline target call. It supports configurable `required_calls` and `max_calls`.

Per-call deterministic outcomes:

- target finding still present or target MATCH -> `FIX_FAILED`;
- new blocking finding -> `FIX_REGRESSION`;
- environment not comparable or business-health evidence insufficient -> inconclusive observation;
- target gone, call is NO_MATCH for the original fault, environment comparable and business checks healthy -> successful verification call.

Overall results:

- enough successful calls -> `FIX_VERIFIED` -> Case `RESOLVED`;
- failure -> `FIX_FAILED` and diagnosis is reopened;
- max calls exhausted without enough successful calls -> `FIX_INCONCLUSIVE` and diagnosis is reopened;
- blocking regression -> `FIX_REGRESSION`, remains in resolving/triage flow and is never marked resolved.

Each verification call produces immutable `FIX_COMPARISON` L1 derived evidence with lineage back to the baseline and verification evidence. The FIX_VERIFICATION DiagnosticQuestion is answered only after `FIX_VERIFIED`.

## 8. Persistence and migration

Migration: `0010_phase_c3_diagnostic_experiments.py`

C3 adds/extends persistence for:

- DiagnosticQuestion
- DiagnosticExperiment
- ExperimentRun
- ExperimentEnvironmentSnapshot
- EnvironmentComparison
- CausalAssessment
- FixAction
- FixVerificationRun

The Alembic chain was also corrected so C2 migration `0009_reproduction_evidence_capture` down-revises the actual C1 revision ID `0008`.

`alembic heads` now reports one head:

```text
0010_phase_c3_diagnostic_experiments (head)
```

PostgreSQL migration runtime is not claimed as verified in this environment because no PostgreSQL/Docker runtime is available. A full SQLite upgrade is not a valid substitute because an older PostgreSQL-oriented migration (`0006`) uses ALTER CONSTRAINT operations unsupported by SQLite.

## 9. API and RBAC

C3 adds API surfaces for:

- diagnostic question templates and Case questions;
- deterministic question answer requests;
- experiment profile discovery;
- experiment create/read/runs/next-run;
- external action completion;
- experiment reproduction start/result attachment;
- environment comparisons and causal assessments;
- FixAction creation;
- FixVerification create/evaluate/read.

Experiment/Fix read/control permissions are server-side RBAC permissions. Create/evaluate operations that can be retried support the existing idempotency mechanism.

## 10. Validation

Validated C3 source state:

- Backend tests: **124/124 PASS**
- Reproduction profiles: **8/8 PASS**
- DiagnosticQuestion templates: **17 PASS**
- Experiment profiles: **6/6 PASS**
- C1 Mock Reproduction E2E regression: **3/3 PASS**
- C2 Evidence E2E regression: **5/5 PASS**
- C3 Diagnostic Experiment E2E: **4/4 PASS**
- Rules: **11/11 PASS**
- Synthetic Golden: **21/21 PASS**
- Synthetic E2E: **53/53 PASS**
- Baseline regression diff: **0 regressions / 0 changes**
- APF1250 Field Golden: **15/15 PASS**
- Python compile: **PASS**
- Alembic single-head graph: **PASS**
- Docker full-stack runtime: **UNVERIFIED (Docker CLI/daemon unavailable)**
- PostgreSQL migration runtime: **UNVERIFIED (PostgreSQL runtime unavailable)**

C3 E2E scenarios:

1. `POWER_SUPPLY_ABA_CAUSAL_CONFIRM` -> ROOT_CAUSE_CONFIRMED
2. `HARD_DRIFT_RETRY_RECOVERY` -> invalid B, clean B retry, STRONGLY_SUPPORTED
3. `POST_REBOOT_REPEAT_MATCH` -> ROOT_CAUSE_CONFIRMED, no real reboot command executed
4. `FIX_VERIFICATION_TWO_CALLS` -> FIX_VERIFIED, Case RESOLVED

## 11. EC-02 boundary

EC-02 remains intentionally RESERVED. C3 does not implement or guess:

- real Voice VLAN/Gateway resolver commands;
- AIM Debug ON/OFF commands;
- OFFHOOK/ONHOOK real event sources;
- real PCM diagnostic control commands;
- reboot commands;
- any other unapproved DUT action.

The Mock Platform and external experiment inputs keep C3 testable without violating the frozen platform-contract boundary.

## 12. Remaining work after C3

C3 completes the deterministic diagnostic-experiment/causal/fix-verification backend core. Remaining V1 work is primarily:

- EC-02 real DUT Platform Contract and adapter binding;
- Web/Feishu surfaces for experiment instructions, question depth, causal evidence and fix verification;
- real PostgreSQL/Docker full-stack migration/runtime verification;
- real DUT/field experiment validation once EC-02 is frozen.

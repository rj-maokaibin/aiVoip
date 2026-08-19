# AI3 Case Copilot V1 — Implementation Trace

## 1. Goal and boundary

AI3 is a read-only Case Copilot for natural-language questions about the current VOIP Case. It summarizes current evidence, explains analyzer/report state, exposes uncertainty, and may recommend read-only next steps.

AI3 is **not** an execution agent. It cannot start/stop reproduction, run a device command, execute an experiment/fix, change Case association, promote Evidence Level, or confirm Root Cause.

Execution and authority remain:

- G1 Case Resolver -> Case association authority
- G2 Identity / RBAC / Case ACL -> authorization authority
- deterministic Router / Policy / Orchestrator -> execution authority
- deterministic confirmation / human review / fix verification -> Root Cause authority

## 2. Persistence and idempotency isolation

Migration: `0025_ai_case_copilot_v1`

Model: `AICaseCopilotRecord`

Persisted state includes:

- Case / request key / actor id / actor role
- SHA256 question hash, not raw question text
- Case Snapshot fingerprint
- response status
- structured proposal
- Claim Grounding report
- routed control Intent if applicable
- prompt/model/error metadata

`request_key` is unique, but uniqueness alone is not treated as an authorization boundary.

### API idempotency

API idempotency is scoped to the effective authorization context using a fixed-length key:

`SHA256(case_id + actor_id + actor_role + request_id)`.

The Service independently validates an existing record before replay:

- same Case;
- same actor;
- same role;
- same question hash.

A conflict fails closed with one of:

- `COPILOT_REQUEST_KEY_CASE_CONFLICT`
- `COPILOT_REQUEST_KEY_ACTOR_CONFLICT`
- `COPILOT_REQUEST_KEY_ROLE_CONFLICT`
- `COPILOT_REQUEST_KEY_QUESTION_CONFLICT`

This prevents an answer projected for an Engineer from being replayed to a Viewer and prevents reusing the same request key for changed question content.

### Feishu idempotency

Feishu event replay protection is scoped **before** the generic idempotency layer can return a cached response:

`SHA256(tenant_key + case_id + actor_id + actor_role + delivery_id)`.

The same Feishu `message_id/event_id` in two different tenants therefore produces independent idempotency records and independent G2/AI3 processing. Raw tenant/actor values are not embedded in the stored key.

## 3. Current-Case intelligence snapshot

`CaseIntelligenceSnapshotBuilder` extends the existing deterministic `CaseEvidenceSnapshotBuilder` and keeps AI3 scoped to the current Case.

It includes current Case:

- status / summary
- devices
- Evidence and latest analyzer summaries
- latest Case Preliminary Evidence Report + Findings
- latest Diagnosis state
- recent Reproduction Sessions
- registered Diagnostic Experiments
- Fix Verification Runs
- authority metadata
- source fingerprint

No cross-Case similarity/history is used as current fact in AI3 V1.

### Role projection

- Viewer receives only report/derived Evidence; **RAW Evidence rows are removed from the Viewer snapshot entirely**.
- Viewer analyzer input Evidence references are filtered to the same authorized visible set.
- Engineer / Expert Reviewer / Admin / Service may receive engineering Evidence view internally.
- Direct device access identifiers are removed again before Reasoning Gateway transport.
- Fix Verification Evidence references are hidden from Viewer when the underlying Evidence is not visible to that role.

## 4. Gateway contract

`CaseCopilotGatewayClient` sends a compact, redacted Case snapshot and an explicit read-only policy:

- current Case only
- every diagnostic claim must cite current-Case Evidence
- AI claims are L5 `PROPOSED` only
- Root Cause confirmation forbidden
- Evidence Level promotion forbidden
- raw device commands forbidden
- control requests must return to deterministic control Intent path

Device metadata uses an explicit allowlist (`product/model/version/software_version/firmware_version/hardware_version/platform`) **before** generic recursive redaction. IP/MAC/SN/credentials/custom secret keys are therefore not allowed to rely on regex detection alone.

Reasoning Gateway payload safety/redaction is reused from the existing diagnosis gateway.

## 5. Grounded answer contract

Schema: `ai-case-copilot-v1`

A proposal must contain:

- answer text
- **at least one** structured `DiagnosticClaim`
- **at least one** public Evidence citation
- uncertainty
- read-only or control-routing next steps
- `root_cause_confirmed_by_ai=false`
- `safety_class=READ_ONLY_GROUNDED_RESPONSE`

The contract rejects executable command material and explicit final Root Cause confirmation wording. Empty-claim prose is rejected so a model cannot bypass structural Grounding by returning a fluent answer with no Claims.

## 6. Claim Grounding authority

AI3 reuses `ClaimGroundingValidator` rather than creating a second evidence authority model.

For AI-generated claims the existing validator requires:

- referenced Evidence belongs to the allowed current-Case Evidence set
- claim status remains `PROPOSED`
- Evidence Level remains L5
- a support Evidence reference exists
- contradictions are surfaced

AI3 additionally requires:

- every public citation belongs to the role-authorized current-Case Evidence set
- every public citation is bound to a structured Claim
- every Evidence reference used by a Claim appears in the public citation set
- if the requester has **zero authorized Evidence**, AI3 fails closed with `COPILOT_NO_AUTHORIZED_EVIDENCE` **without calling the model**

A non-PASS grounding result causes the response to be rejected and not shown as an AI answer.

## 7. Control-request isolation

Before the model is called, AI3 detects control requests such as:

- STOP_REPRODUCTION
- EXTERNAL_ACTION_COMPLETED
- FIX_APPLIED
- starting/running a registered experiment

The result is `CONTROL_INTENT_REQUIRED`; no model/action worker is called. The caller must re-enter deterministic Intent -> G2 RBAC/Case ACL -> Policy -> Orchestrator.

`CaseCopilotService` deliberately has no reproduction/experiment/fix task dispatcher.

## 8. Runtime failure isolation

AI3 is optional read-only infrastructure and must not poison the deterministic transaction.

### Feishu

The Case Copilot call executes inside a SQLAlchemy nested transaction/SAVEPOINT. If an unexpected model/grounding/persistence exception escapes the normal AI3 error contract:

- only AI3 side effects inside the SAVEPOINT are rolled back
- G1/G2 parent transaction remains usable
- `AI_CASE_COPILOT_RUNTIME_FAILED` is audited using only the error class/code, never raw question text
- user receives a safe fallback reply once
- Feishu event idempotency is completed, so redelivery does not repeatedly call the failing sidecar

### API

The read-only Copilot API uses the same SAVEPOINT boundary. An unexpected AI3 runtime failure:

- rolls back only AI3 side effects
- records safe audit metadata
- commits the audit while preserving the Case transaction
- returns HTTP 503 `AI_CASE_COPILOT_RUNTIME_FAILED`

## 9. API

`POST /api/v1/cases/{case_id}/ai/copilot`

Requirements:

- `CASE_READ` + `REPORT_READ`
- feature flag `AI_CASE_COPILOT_ENABLED=true`
- role-aware Snapshot projection

The API always reports:

- `read_only=true`
- Root Cause authority is deterministic/human-confirmed only
- execution authority is deterministic Router/RBAC/Policy/Orchestrator

Rejected or Gateway-failed proposals are replaced by safe explanatory text rather than exposing an ungrounded answer.

## 10. Feishu integration

AI3 is integrated only in the G2 `Authorized Event Gateway`.

For an authorized, Case-bound `GENERAL_QUESTION` without attachments:

1. G2 Identity/RBAC authorization has already passed.
2. G1 resolves the current Case.
3. Tenant/Case/actor/role-scoped idempotency is established.
4. AI3 builds role-aware current Case Snapshot.
5. Grounded read-only answer is generated inside the AI3 SAVEPOINT.
6. Reply is written once with the same scoped idempotency record.

Unknown/disabled users never reach Copilot. A recognized control message continues through the existing deterministic business handler rather than AI3.

When AI3 is disabled or no Case is resolved, existing verified-knowledge behavior remains unchanged.

## 11. Tests and software gate

Focused tests cover:

- Viewer cannot see RAW Evidence; Engineer can
- current-Case grounded answer PASS
- cross-Case Evidence rejection
- AI Evidence Level/status promotion rejection
- Root Cause confirmation rejection
- no authorized Evidence -> reject without model
- empty Claim prose -> reject
- Claim Evidence missing from public citations -> reject
- control request bypasses model/action worker
- same-actor request-key idempotency
- cross-actor request-key replay rejected
- cross-role request-key replay rejected
- same request key with different question rejected
- API idempotency key bounded and does not expose raw actor id
- Gateway IP/MAC/SN/password/question-secret redaction + device metadata allowlist
- API feature gate + read-only authority response
- API SAVEPOINT runtime failure isolation
- authorized Feishu Case question -> Copilot
- unknown identity denied before Copilot
- duplicate Feishu event replies once
- identical Feishu delivery ID in different tenants does not cross-replay
- Feishu idempotency key is scoped to tenant/Case/actor/role and bounded SHA-256
- Feishu SAVEPOINT runtime failure isolation and safe replay
- control Feishu message stays on deterministic control path

Repository CI has a dedicated `AI3 Case Copilot software gate` in addition to the full backend regression. The dedicated gate now includes:

- `test_ai3_copilot_idempotency_isolation_v1.py`
- `test_ai3_feishu_tenant_idempotency_v1.py`

## 12. Static acceptance defects found and fixed

Pre-CI acceptance found two real security defects and fixed them rather than classifying them as environment Pending:

1. **API cross-authorization replay risk** — `case_id + request_id` could replay an Engineer-projected answer to another actor/role. Fixed with scoped SHA-256 API key plus independent Service validation of Case/actor/role/question hash.
2. **Feishu cross-Tenant idempotency replay risk** — generic idempotency previously keyed only by `message_id/event_id`, allowing a theoretical collision to replay before Service validation. Fixed by scoping the key to tenant/Case/actor/role/delivery ID.

## 13. Current validation status

PR #9 remains Draft.

GitHub-hosted Actions currently fails before any workflow step is created (`steps=null`, `logs_url=null`), including reruns and minimal runner probes. Therefore the latest idempotency-isolation commits have not yet received executable CI evidence and **AI3 software PASS is not claimed**.

Before PR #9 may become Ready, the latest head must execute and PASS:

- Python compile;
- AI Contract Coverage Gate;
- AI E1–E6 regression;
- AI1 Semantic Router gate;
- dedicated AI3 gate including both new replay-isolation tests;
- M7 acceptance contract;
- PostgreSQL clean migration through `0025_ai_case_copilot_v1`;
- full backend regression;
- Preliminary Evidence Report software release gate;
- frontend dependency audit and production build.

## 14. Remaining production acceptance

AI3 software completion does not itself satisfy production acceptance. Required live gates include:

- live Reasoning Gateway observability/error isolation
- real Case question corpus grounded answer correctness
- unauthorized Evidence disclosure = 0
- cross-Case Evidence reference = 0
- unsupported/ungrounded Claim release = 0
- Root Cause authority violation = 0
- control action direct-execution from Copilot = 0

These live checks belong to the existing external acceptance categories and do not excuse any software Gate failure.

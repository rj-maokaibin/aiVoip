# AI3 Case Copilot V1 — Implementation Trace

## 1. Goal and boundary

AI3 is a read-only Case Copilot for natural-language questions about the current VOIP Case. It summarizes current evidence, explains analyzer/report state, exposes uncertainty, and may recommend read-only next steps.

AI3 is **not** an execution agent. It cannot start/stop reproduction, run a device command, execute an experiment/fix, change Case association, promote Evidence Level, or confirm Root Cause.

Execution and authority remain:

- G1 Case Resolver -> Case association authority
- G2 Identity / RBAC / Case ACL -> authorization authority
- deterministic Router / Policy / Orchestrator -> execution authority
- deterministic confirmation / human review / fix verification -> Root Cause authority

## 2. Persistence

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

`request_key` is unique so API/Feishu redelivery cannot produce a second model interaction.

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

- Viewer receives report-level Evidence/analyzer summaries only.
- Engineer / Expert Reviewer / Admin / Service may receive engineering Evidence view internally.
- Direct device access identifiers are removed again before Reasoning Gateway transport.

## 4. Gateway contract

`CaseCopilotGatewayClient` sends a compact, redacted Case snapshot and an explicit read-only policy:

- current Case only
- every diagnostic claim must cite current-Case Evidence
- AI claims are L5 `PROPOSED` only
- Root Cause confirmation forbidden
- Evidence Level promotion forbidden
- raw device commands forbidden
- control requests must return to deterministic control Intent path

Reasoning Gateway payload safety/redaction is reused from the existing diagnosis gateway.

## 5. Grounded answer contract

Schema: `ai-case-copilot-v1`

A proposal contains:

- answer text
- structured `DiagnosticClaim` list
- cited Evidence IDs
- uncertainty
- read-only or control-routing next steps
- `root_cause_confirmed_by_ai=false`
- `safety_class=READ_ONLY_GROUNDED_RESPONSE`

The contract rejects executable command material and explicit final Root Cause confirmation wording.

## 6. Claim Grounding authority

AI3 reuses `ClaimGroundingValidator` rather than creating a second evidence authority model.

For AI-generated claims the existing validator requires:

- referenced Evidence belongs to the allowed current-Case Evidence set
- claim status remains `PROPOSED`
- Evidence Level remains L5
- a support Evidence reference exists
- contradictions are surfaced

AI3 additionally validates that every public citation belongs to current Case Evidence and is bound to a structured Claim.

A non-PASS grounding result causes the response to be rejected and not shown as an AI answer.

## 7. Control-request isolation

Before the model is called, AI3 detects control requests such as:

- STOP_REPRODUCTION
- EXTERNAL_ACTION_COMPLETED
- FIX_APPLIED
- starting/running a registered experiment

The result is `CONTROL_INTENT_REQUIRED`; no model/action worker is called. The caller must re-enter deterministic Intent -> G2 RBAC/Case ACL -> Policy -> Orchestrator.

`CaseCopilotService` deliberately has no reproduction/experiment/fix task dispatcher.

## 8. API

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

## 9. Feishu integration

AI3 is integrated only in the G2 `Authorized Event Gateway`.

For an authorized, Case-bound `GENERAL_QUESTION` without attachments:

1. G2 Identity/RBAC authorization has already passed.
2. G1 resolves the current Case.
3. AI3 builds role-aware current Case Snapshot.
4. Grounded read-only answer is generated.
5. Reply is written once with an idempotency record.

Unknown/disabled users never reach Copilot. A recognized control message continues through the existing deterministic business handler rather than AI3.

When AI3 is disabled or no Case is resolved, existing verified-knowledge behavior remains unchanged.

## 10. Tests and software gate

Focused tests cover:

- Viewer vs Engineer Snapshot visibility
- current-Case grounded answer PASS
- cross-Case Evidence rejection
- AI Evidence Level/status promotion rejection
- Root Cause confirmation rejection
- control request bypasses model/action worker
- request-key idempotency
- Gateway IP/MAC/SN/password/question-secret redaction
- API feature gate + read-only authority response
- authorized Feishu Case question -> Copilot
- unknown identity denied before Copilot
- duplicate Feishu event replies once
- control Feishu message stays on deterministic control path

Repository CI has a dedicated `AI3 Case Copilot software gate` in addition to the full backend regression.

## 11. Remaining production acceptance

AI3 software completion does not itself satisfy production acceptance. Required live gates include:

- live Reasoning Gateway observability/error isolation
- real Case question corpus grounded answer correctness
- unauthorized Evidence disclosure = 0
- cross-Case Evidence reference = 0
- unsupported/ungrounded Claim release = 0
- Root Cause authority violation = 0
- control action direct-execution from Copilot = 0

These gates must be measured on real, reviewed Case traffic before broad production enablement.

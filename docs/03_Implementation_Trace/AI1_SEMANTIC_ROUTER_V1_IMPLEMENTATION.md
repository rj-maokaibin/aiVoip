# AI1 Semantic Router V1 — Implementation Trace

## 1. Goal and authority boundary

AI1 understands complex Feishu natural language and emits a strict structured Intent Proposal. It does **not** replace the deterministic router in V1 and has no execution authority.

Frozen authority order remains:

Feishu message -> Identity -> G1 Case Resolver -> deterministic Intake Router -> AI1 SHADOW proposal (when eligible) -> G2 Capability/RBAC/Case ACL -> deterministic business handler.

V1 `final_intent` is always the deterministic intent. AI1 cannot override Case association, authorize an action, execute a device command, confirm Root Cause, or elevate Evidence Level.

## 2. Contract

Schema: `feishu-semantic-intent-v1`

Implemented in `backend/app/contracts/semantic_intent.py` with `extra=forbid`.

Fields cover intent/case operation/requested operation, Case reference, symptoms/device references, environment changes, temporal clues, attachment roles, comparison request, confidence/missing fields, and fixed safety class `NON_EXECUTING_PROPOSAL`.

There is deliberately no raw command/action parameter field. A second validation guard rejects command-like material embedded inside free-form semantic values.

## 3. Persistence and tenant-scoped idempotency

Migration: `0024_ai_semantic_router_v1`

Model: `AISemanticIntentRecord`

The existing database `UNIQUE(message_id)` contract is retained, but the stored column is now a bounded, privacy-preserving **semantic delivery key** rather than the raw Feishu delivery ID:

- live Feishu traffic: `sem:SHA256(tenant_key + message_id)`
- tenant-less Admin/debug traffic: `sem:SHA256(case_id + message_id)`

This is necessary because a Feishu message ID is an event-delivery identifier inside its tenant boundary; global `message_id` replay before tenant validation could otherwise reuse another tenant's SHADOW record.

Important invariants:

- persisted delivery key remains globally unique and fixed length
- raw tenant key is not embedded in the unique key
- raw Feishu text is not persisted in the AI record; only SHA256 input hash + structured proposal
- raw source message ID is retained only in the surrounding Feishu/Audit flow where required, not in `AISemanticIntentRecord.message_id`
- deterministic intent/confidence are stored beside AI proposal for SHADOW evaluation
- statuses: `SHADOW_VALID`, `REJECTED`, `BYPASSED`, `GATEWAY_FAILED`
- duplicate delivery inside the same tenant reuses one semantic record and does not call the model again
- identical source message IDs in two different tenants create independent semantic records and cannot cross-replay

No new migration revision is required because the column type/length/unique constraint are unchanged; only the value semantics are hardened.

## 4. Hybrid router

`needs_semantic_fallback()` keeps deterministic-first behavior:

- explicit STOP/External Action/Fix completion -> deterministic only
- high-confidence status -> deterministic only
- obvious high-confidence simple diagnosis/attachment -> deterministic only
- environment changes / A-B comparisons / complex follow-up -> semantic eligible
- unsupported, ambiguous or lower-confidence follow-up/question -> semantic eligible

AI1 currently supports runtime modes `OFF` and `SHADOW` only. SUGGEST/CONTROLLED routing is intentionally not part of this PR.

## 5. G1 Case authority re-check

AI1 is called only after G1 has resolved the Case in the authorized Feishu message flow. A proposal may leave `case_ref` empty or repeat the already resolved Case ID/Case No; any other Case reference is rejected with `SEMANTIC_CASE_OVERRIDE_FORBIDDEN`.

## 6. Gateway safety

`SemanticGatewayClient` reuses the existing recursive Reasoning Gateway redaction/safety helpers. Gateway payload contains normalized text, attachment metadata, deterministic candidate and minimal resolved Case context.

Policy explicitly states output is non-executing, raw commands forbidden, Case override forbidden, RBAC/Policy re-check mandatory, Root Cause confirmation forbidden, and deterministic router remains execution authority.

IP, MAC, phone/number and secret material are redacted before transport; gateway bearer token exists only in the HTTP header. Payload safety failures are converted to `SemanticGatewayError`; the SHADOW sidecar cannot crash the deterministic Feishu workflow.

## 7. Fail-closed behavior

The semantic proposal is rejected when schema validation fails, extra/execution fields are emitted, command material is detected, model attempts Case override, confidence is below threshold, or gateway output is invalid. Gateway transport/safety failure is recorded as `GATEWAY_FAILED`. None changes the deterministic workflow route.

## 8. API

Admin/Service debug API:

`POST /api/v1/cases/{case_id}/ai/semantic/resolve`

It returns deterministic intent, semantic proposal/status and record ID. It is non-executing and always reports deterministic Router/RBAC/Policy as execution authority.

## 9. Evaluation and tests

Focused tests cover:

- deterministic control bypass
- complex environment/A-B semantic eligibility
- SHADOW proposal cannot change final intent
- G1 Case override rejection
- low-confidence fail closed
- duplicate delivery idempotency within one tenant
- same source message ID in two tenants cannot cross-replay
- tenant-less manual/debug message ID is scoped by Case
- persisted semantic delivery key is hashed/bounded and does not expose raw tenant identity
- raw-command/extra-field rejection
- Gateway IP/MAC/number/secret redaction
- Admin/Service non-executing debug API
- synthetic contract corpus routing gate

The existing `test_ai1_semantic_router_v1.py` was extended with the tenant-isolation contract, so the dedicated `AI1 Semantic Router software gate` automatically covers the hardening.

### Real-corpus executable acceptance

`tools/ai1_semantic_eval.py` emits `ai1-semantic-eval-v1` with PASS/FAIL for Intent Accuracy >=95%, dangerous false allow=0, wrong Case association=0, and fail-closed rate=100%.

The evaluator itself remains a software gate; production acceptance must run it on reviewed/de-identified real Feishu traffic labels.

## 10. Static acceptance defect found and fixed

During system-level acceptance after AI3 tenant-replay review, a real AI1 defect was found:

**AI1 cross-Tenant semantic replay risk** — `shadow_semantic_route()` previously checked and persisted a globally unique raw `message_id` before tenant-specific isolation. Identical message IDs in two tenants could therefore reuse/violate another tenant's semantic record. The persisted key is now tenant-scoped SHA-256, with dedicated regression coverage.

This is a software fix, not an external environment Pending.

## 11. Current validation status

The historical AI1 head `74d9c18b6f761efaa6747b77a62c223074773d63` passed workflow #324, including compile, AI1 focused tests, migration through 0024, 501 backend tests, Evidence Report gate and frontend build.

**That historical PASS does not certify the new tenant-isolation head.** The latest AI1 hardening commits require a fresh executable CI run. GitHub-hosted Actions is currently failing before any step is created (`steps=null`, `logs_url=null`), so AI1 latest-head software PASS is temporarily not claimed.

Before the updated PR #8 may be considered machine-validated again, latest head must execute and PASS the normal repository gate, including the extended AI1 test file and full regression.

## 12. Remaining production acceptance

Before AI1 can influence routing beyond SHADOW:

- real reviewed Feishu corpus evaluation PASS
- live Gateway observability/audit verified
- dangerous false allow remains 0
- Case wrong association remains 0

Promotion beyond SHADOW requires a separate explicit change and Promotion Gate.

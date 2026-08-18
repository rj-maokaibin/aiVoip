# AI1 Semantic Router V1 — Implementation Trace

## 1. Goal and authority boundary

AI1 understands complex Feishu natural language and emits a strict structured Intent Proposal. It does **not** replace the deterministic router in V1 and has no execution authority.

Frozen authority order remains:

Feishu message -> Identity -> G1 Case Resolver -> deterministic Intake Router -> AI1 SHADOW proposal (when eligible) -> G2 Capability/RBAC/Case ACL -> deterministic business handler.

V1 `final_intent` is always the deterministic intent. AI1 cannot override Case association, authorize an action, execute a device command, confirm Root Cause, or elevate Evidence Level.

## 2. Contract

Schema: `feishu-semantic-intent-v1`

Implemented in `backend/app/contracts/semantic_intent.py` with `extra=forbid`.

Fields cover:

- intent / case operation / requested operation
- case reference
- symptoms / device references
- environment changes
- temporal clues
- attachment roles
- comparison request
- confidence / missing fields
- fixed safety class `NON_EXECUTING_PROPOSAL`

There is deliberately no raw command/action parameter field. A second validation guard rejects command-like material embedded inside free-form semantic values.

## 3. Persistence and idempotency

Migration: `0024_ai_semantic_router_v1`

Model: `AISemanticIntentRecord`

Important invariants:

- `message_id` unique
- raw Feishu text is not persisted in the AI record; only SHA256 input hash + structured proposal
- deterministic intent/confidence are stored beside AI proposal for Shadow evaluation
- statuses: `SHADOW_VALID`, `REJECTED`, `BYPASSED`, `GATEWAY_FAILED`
- duplicate message delivery reuses the single semantic record and does not call the model again

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

This guarantees Case wrong-association cannot be introduced by the model.

## 6. Gateway safety

`SemanticGatewayClient` reuses the existing recursive Reasoning Gateway redaction/safety helpers.

Gateway payload contains normalized text, attachment metadata, deterministic candidate and minimal resolved Case context. Policy explicitly states:

- output is non-executing
- raw commands forbidden
- Case override forbidden
- RBAC/Policy re-check mandatory
- Root Cause confirmation forbidden
- deterministic router remains execution authority

IP, MAC, phone/number and secret material are redacted before transport; gateway bearer token exists only in the HTTP header. Payload safety failures are converted to `SemanticGatewayError`; the SHADOW sidecar cannot crash the deterministic Feishu workflow.

## 7. Fail-closed behavior

The semantic proposal is rejected when:

- JSON/Pydantic schema validation fails
- extra/execution fields are emitted
- command material is detected
- model attempts Case override
- confidence is below configured threshold
- gateway returns invalid data

Gateway transport/safety failure is recorded as `GATEWAY_FAILED`. None of these conditions changes the deterministic workflow route.

## 8. API

Admin/Service debug API:

`POST /api/v1/cases/{case_id}/ai/semantic/resolve`

It returns deterministic intent, semantic proposal/status and the semantic record ID. It is explicitly non-executing and always reports deterministic Router/RBAC/Policy as execution authority.

## 9. Evaluation and tests

Focused tests cover:

- deterministic control bypass
- complex environment/A-B semantic eligibility
- SHADOW proposal cannot change final intent
- G1 Case override rejection
- low-confidence fail closed
- duplicate message id idempotency
- raw-command/extra-field rejection
- Gateway IP/MAC/number/secret redaction
- Admin/Service non-executing debug API
- synthetic contract corpus routing gate

The CI workflow has a dedicated `AI1 Semantic Router software gate` so these invariants fail independently from the large backend regression.

The synthetic corpus is a software-contract gate, **not** a substitute for the frozen production acceptance target.

### Real-corpus executable acceptance

`tools/ai1_semantic_eval.py` accepts reviewed/de-identified JSON or JSONL labels and emits `ai1-semantic-eval-v1` with machine-verifiable PASS/FAIL for:

- Intent Accuracy >= 95%
- Dangerous Intent false allow = 0
- Case wrong association = 0
- invalid/low-confidence/gateway-failure Fail-Closed Rate = 100%

The evaluator itself has positive/negative threshold tests and is included in the dedicated AI1 CI gate. Production acceptance must run this tool on real Feishu traffic labels; synthetic data cannot satisfy that environment gate.

## 10. Release acceptance still required

Before AI1 can influence real routing beyond SHADOW:

- run `tools/ai1_semantic_eval.py` against reviewed real Feishu corpus and obtain PASS
- live Gateway observability/audit verified
- Dangerous Intent false allow remains 0 under live traffic
- Case wrong association remains 0 under live traffic

Promotion beyond SHADOW requires an explicit later change and corresponding Promotion Gate; this PR does not implement that promotion.

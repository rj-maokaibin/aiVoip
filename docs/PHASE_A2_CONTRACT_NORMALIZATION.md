# VOIP AI V1.0 — Phase A2 Contract Normalization

Status: IMPLEMENTED / VALIDATED (offline + APF1250 field golden)

This increment continues the frozen V1.0 Engineering Contract alignment on top of Phase A1. Its goal is to make persistence, lifecycle, authorization, pagination, idempotency, audit, and event behavior deterministic before M6.2 autonomous reproduction is implemented.

## Contract areas strengthened

### EC-00 — global registry / errors / identity

- Added central `ActorType`, `PermissionName`, and `EventType` registries.
- Added frozen server-side permission matrix for VIEWER / ENGINEER / EXPERT_REVIEWER / ADMIN / SERVICE.
- Normalized API errors into the EC-00 error envelope, including request validation, HTTP errors, and unhandled internal errors.
- Trace ID continues through the error contract; internal exceptions are not exposed to clients.
- Added contract error codes for dependency conflicts, cycles, cross-Case dependencies, invalid cursor, validation failures, and routing failures.

### EC-01 — Case / Job / Evidence lifecycle

- Case state history now records transition event, actor, and context.
- Added append-only `JobStateHistory`.
- Job state mutation is centralized through the Job transition service; workers no longer write `job.status` directly.
- Job execution checks declared dependencies before entering RUNNING.
- Job dependency creation rejects cross-Case dependencies, cycles, conflicting duplicate semantics, and mutation of non-PENDING child jobs.
- Job cancellation remains safety-aware and does not bypass Cleanup requirements.

### EC-10 — persistence normalization

- Added Alembic migration `0007_contract_normalization`.
- Normalized Case state history, Job state history, and Audit persistence needed by the frozen contract.
- Added query indexes for state/history/audit paths introduced in this phase.
- No speculative M2–M6.2 semantic columns were invented where the frozen machine-level schema is not yet explicit.

### EC-11 — REST / SSE / OpenAPI behavior

- Added stable opaque cursor pagination with `items / next_cursor / has_more`.
- Cursor pagination is now used by Case, Evidence, Job, AnalyzerRun, Report, and Audit list endpoints implemented in this increment.
- Added Job dependency read/create APIs.
- Extended Idempotency-Key coverage to evidence upload, job cancellation, hypothesis confirmation, rule mutation/activation/replay, knowledge creation/verification, and report generation.
- SSE events use the central EventType registry.
- Frontend and full-stack E2E helper were updated to consume the cursor page envelope and canonical Job terminal states.

### EC-13 — RBAC / audit

- Added server-side `require_permissions` enforcement; authorization is not delegated to frontend button visibility.
- Rule / Knowledge / Report writes use the authenticated actor rather than trusting an arbitrary request actor value.
- Audit records now carry normalized actor type, action, target, before, after, reason, trace ID, Case ID, and details.
- Audit creation also publishes the corresponding event into the event outbox.

## Tests added

`backend/tests/test_contract_normalization_a2.py` covers:

- server-side permission registry;
- dependency enforcement before Job RUNNING;
- append-only Job history;
- cross-Case and dependency-cycle rejection;
- Case history event/actor/context;
- normalized Audit + event outbox;
- stable cursor pagination.

## Validation result

- Backend unit tests: **84 / 84 passed**.
- Rule validation: **11 / 11 passed**.
- Synthetic Golden: **5 / 5 cases, 21 / 21 checks passed**.
- Synthetic E2E: **10 / 10 cases, 53 / 53 checks passed**.
- Baseline diff: **0 regressions, 0 observed changes**.
- APF1250 field Golden: **15 / 15 checks passed**; field analysis remains `PARTIAL_SUCCESS` as expected by the case manifest.
- Python source and Alembic migration compilation: **passed**.
- Docker full-stack runtime: **UNVERIFIED** because the execution environment has no Docker CLI/daemon.

## Explicitly still not implemented / not claimed

- **EC-02 remains RESERVED / PENDING_PLATFORM_CONTRACT.** No real Voice Gateway resolver command, real AIM debug ON/OFF mapping, realtime OFFHOOK/ONHOOK source, or other unconfirmed DUT command was invented.
- **M6.2 Reproduction Intelligence is not implemented yet.** This increment prepares the deterministic platform-independent foundation only.
- Full relational persistence for every M2–M6.2 semantic object is not claimed complete. Missing machine-level contract details must become `CONTRACT_GAP`, not guessed schema.
- Production IdP/OIDC integration is not implemented; current runtime identity remains the existing header/development mechanism with server-side authorization enforcement.
- Runtime OpenAPI snapshot generation was not validated in the host environment because the host Python environment does not have the project Celery runtime dependency installed. API schemas and tests do pass in the available test environment.
- Audio Analyzer thresholds are not yet fully externalized into versioned AnalyzerProfile YAML; this is a Phase B item.
- Safe worker-specific cancellation/Cleanup execution remains to be completed where a running diagnostic operation owns external state.

## Next implementation order

1. **Phase B:** harden SIP/RTP/PCM/audio semantic contracts and externalize AnalyzerProfile thresholds/configuration.
2. **Phase C:** implement M6.2 platform-independent core against Mock Platform only.
3. **Phase D:** complete EC-02 from verified DUT facts, then bind M6.2 to the real platform.
4. **Phase E/F:** Web/Feishu/knowledge closure, real Docker full-stack verification, and final Release Gate.

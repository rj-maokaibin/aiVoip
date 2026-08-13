# VOIP AI V1.0 — Phase A1 Contract Foundation

Status: IMPLEMENTED / VALIDATED (offline + field golden)

This increment aligns the existing M0–M6.1 codebase with the highest-priority parts of the frozen V1.0 Engineering Contract before M6.2 is built.

## Implemented

- EC-00 central enum registry for Case/Job/Run/Evidence/Hypothesis/Rule/RBAC/error domains.
- Frozen HTTP error envelope with trace ID propagation.
- API identity/RBAC dependency foundation with development-only fallback.
- Idempotency records and reusable idempotency service; completed requests replay, conflicting payloads reject, failed/expired requests may safely retry.
- Case event-driven transition service; arbitrary state jumps rejected.
- FIX_VERIFIED guard requires same-Case COMPLETE Evidence whose metadata result is FIX_VERIFIED.
- Job lifecycle service; cancellation cannot become CANCELLED before cleanup verification.
- Job dependency persistence and policy evaluator foundation.
- Evidence contract fields, Raw/Derived distinction, scope/level/completeness, append-only lineage relations and central creation service.
- Raw Evidence Case FK changed from CASCADE to RESTRICT by migration 0006.
- AnalyzerRun config snapshot/checksum/scope/output-evidence lineage fields.
- Append-only HypothesisRevision history; current Hypothesis is only a projection.
- Rule DSL v2 boolean AST (AND/OR/NOT), frozen operators and category ordering; legacy v1 read compatibility only.
- Hypothesis internal vocabulary migrated to OPEN/SUPPORTED/STRONGLY_SUPPORTED/CONFIRMED/CONTRADICTED/REJECTED.
- SSE EventOutbox and /api/v1/events/stream foundation.
- RBAC persistence schema (users/roles/permissions mappings).
- RTP unknown dynamic PT no longer guesses 8 kHz; clock rate/ptime/RFC3550 jitter remain unavailable until SDP/static mapping exists.
- Migration 0006 for the contract foundation.

## Explicitly not implemented in this increment

- EC-02 real DUT Platform/Action command contract remains RESERVED/PENDING_PLATFORM_CONTRACT. No new DUT commands, Voice Gateway resolver, debug-off command, or realtime hook source were guessed.
- M6.2 autonomous Reproduction Intelligence state machine/capture/cleanup/experiment/fix-verification orchestration is not implemented yet.
- Full V1.0 relational schemas for all M2–M6.2 semantic entities are not yet complete.
- Production IdP integration is not implemented; current RBAC runtime is header identity plus development fallback.
- Running-job cancellation still ends at CANCEL_REQUESTED until a worker-specific safe-stop/CleanupManager completes it.
- Audio Analyzer thresholds have not yet been fully moved into versioned AnalyzerProfile YAML; this remains a Phase B contract item.
- Docker full-stack runtime was not verified in this environment because Docker CLI/daemon is unavailable.

## Validation

- Backend unit tests: 78 / 78 passed.
- Rule validation: 11 / 11 active rule YAML files passed.
- Synthetic Golden: 5 / 5 cases, 21 / 21 checks passed.
- Synthetic E2E: 10 / 10 cases, 53 / 53 checks passed.
- Baseline diff: 0 regressions, 0 changes.
- APF1250 field Golden: 15 / 15 checks passed; analysis result PARTIAL_SUCCESS as expected by the field case.
- Python compile checks, including migration 0006: passed.

## Next implementation order

1. Phase A2: close remaining EC-00/01/10/11/13 contract gaps and normalize persistence/API contracts.
2. Phase B: SIP/RTP/PCM/audio semantic contract hardening and AnalyzerProfile externalization.
3. Phase C: implement M6.2 core against Mock Platform only.
4. Phase D: complete EC-02 with real DUT facts, then bind M6.2 to the real platform.
5. Phase E/F: Web/Feishu/knowledge closure and final Release Gate.

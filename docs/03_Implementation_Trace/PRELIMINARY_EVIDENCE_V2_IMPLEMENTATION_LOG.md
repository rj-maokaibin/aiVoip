# Preliminary Evidence Report V2 Implementation Log

Status: IN PROGRESS

## Time tracking

- Implementation start: `2026-09-03T14:59:01+08:00`
- Timezone: `UTC+08:00`
- Start master SHA: `c8d8d1a12265f319246fb016ae66afaa726b93b7`
- Start master change: `Merge PR #117: rerun-safe immutable production source`
- Current completion branch: `feat/evidence-v2-completion-clean`
- Documentation baseline PR: `#118` — merged during implementation as `f32e32d04ee4a164f54222d3673d450db199d32d`

This log is updated during implementation. Overall elapsed time is measured from the start timestamp above through final production acceptance. CI wall-clock and phase timings are recorded separately so CI/CD waiting is not confused with coding elapsed time.

## CI/CD Performance V2.2 baseline observation

Reference PR: `#116`, head `2eeefd91204475a6006b732112398a8348a93f08`.

Observed final successful PR #116 run:

| Gate | Start | End | Wall time | Notes |
|---|---|---|---:|---|
| Source Manifest Gate | 06:05:40Z | 06:06:06Z | 0m26s | GitHub-hosted exact-SHA bundle/manifest path is fast. |
| Full Software Acceptance workflow | 06:05:40Z | 06:14:26Z | 8m46s | Includes pre-job/dependency/scheduling delay. |
| Full Software Acceptance self-hosted job | 06:09:23Z | 06:14:25Z | 5m02s | Actual controlled-runner critical work. |
| Preliminary Evidence V1 Acceptance | 06:05:45Z | 06:14:34Z | 8m49s | Almost all time after startup is waiting for exact-SHA Full Acceptance evidence. |

Full Acceptance phase breakdown on `voip-controlled-linux-01`:

- Frozen PRD/SPEC contracts: ~37s
- Full VOIP AI software release gate: ~3m35s
- Golden #001: ~18s
- Human Evidence Gate: ~5s

Baseline conclusion: V2.2 removed duplicated heavy work, but PR #116 still had roughly 3m43s between workflow creation and the controlled-runner job start.

## Fresh V2.2 measurement — PR #119

Stable measured head: `aa8ee83c586d764907b4bdac63698da08b3980b8`.

| Gate | Start | End | Wall time | Delta vs PR #116 |
|---|---|---|---:|---:|
| Source Manifest Gate | 07:10:09Z | 07:10:34Z | 0m25s | -1s |
| Full Software Acceptance workflow | 07:10:09Z | 07:15:57Z | 5m48s | **-2m58s / ~34% faster** |
| Full Software Acceptance self-hosted job | 07:10:41Z | 07:15:56Z | 5m15s | +13s / ~4% slower |
| Preliminary Evidence V1 Acceptance | 07:10:09Z | 07:16:05Z | 5m56s | **-2m53s / ~33% faster** |

PR #119 controlled-runner phase timings:

- exact-source resolution before controlled job: ~29s; controlled job started ~32s after workflow creation.
- workspace/source setup through TShark preparation: ~16s after job setup began.
- Frozen PRD/SPEC contracts: 46s.
- Full VOIP AI software release gate: 3m33s.
- Prepared-PCAP Golden #001: 18s.
- Human Evidence Gate: 4s.
- Full Acceptance evidence upload: 9s.

### CI/CD V2.2 assessment after fresh code PR

**Optimization is confirmed effective at the workflow/orchestration level.** The previous ~3m43 pre-run delay collapsed to ~32s on PR #119, reducing end-to-end Full Acceptance from 8m46 to 5m48 and Preliminary Evidence acceptance from 8m49 to 5m56. The actual controlled-runner workload remains approximately five minutes and did not materially improve; therefore the next performance opportunity is inside the controlled acceptance workload, not by weakening exact-source/governance gates.

No Source Manifest, Full Acceptance, Runtime Verify, Exact Source Binding, Golden or V1 compatibility gate was weakened for this improvement.

## Implementation milestones

| Milestone | Status | Started | Completed | Elapsed | Notes |
|---|---|---|---|---|---|
| M0 V2 docs/CR baseline | DONE | before implementation | 2026-09-03 15:13 +08 | - | PR #118 merged as `f32e32d...` |
| M1 Golden #002 + Call/Timeline foundation | DONE | 2026-09-03 14:59 +08 | 2026-09-03 | - | PR #119 merged; ACK/lifecycle and RTP-observed timeline foundation are on master. |
| M2 Finding events/correlation/visibility | DONE | 2026-09-03 15:14 +08 | 2026-09-03 19:06 +08 | - | Clean PR #124 merged; master checkpoint `abe44751...`; exact-head Source Manifest / Full Acceptance / Preliminary Evidence V1 all PASS. |
| M3 Artifact binding + semantic validator | IN PROGRESS | 2026-09-03 | - | - | Clean completion PR #125; full R001-R015 implementation ported; R014 unknown-loss metric edge fixed with regression. |
| M4 Recommendation + Report UX V2 | IN PROGRESS | 2026-09-03 | - | - | Clean completion PR #125 includes deterministic recommendation, composer, renderer, and AI authority boundary. |
| M5 Shadow/canary/full acceptance | IN PROGRESS | 2026-09-03 | - | - | Runtime adapter/migration foundations are in PR #125; production service/Feishu projection, V2 hard gate, PCAP E2E and rollout acceptance remain to close. |

## Elapsed checkpoints

- `2026-09-03T15:17:14+08:00`: 18m13s since implementation start. M0 merged; M1 CI has a clean measured pass; M2 implementation has started in a stacked PR.
- `2026-09-03T15:22:00+08:00`: 22m59s since implementation start. M1 hardened CANCEL lifecycle semantics; source manifest was regenerated and verified. The manifest self-commit was produced by `github-actions[bot]`, which GitHub records as `action_required` for downstream PR workflows, so this user-authored checkpoint commit intentionally retriggers the authoritative gates on the exact refreshed head.
- `2026-09-03`: PR #124 clean M2 replacement merged to master as `abe44751fcc0d040188c0b9f76b7324cf8c16db3` after exact-head Source Manifest, Full Acceptance and Preliminary Evidence V1 all passed.
- `2026-09-03`: PR #125 clean M3-M5 completion branch established from current master. The first head exposed source-manifest drift before any controlled-runner test could start. The manifest was regenerated by a narrow branch-only helper after the known R014 missing-metric bug was fixed and regression-tested locally (31 focused tests PASS). This user-authored documentation checkpoint intentionally retriggers authoritative PR workflows on the refreshed manifest head; no gate is bypassed or weakened.

## Remaining Production DoD

PR #125 is not release-complete merely when legacy V1 gates pass. Final V2 closure still requires all of the following on exact source:

1. Complete production `evidence_report` service integration with V1 default preserved until rollout.
2. V2 artifact binding in the production artifact path, including `ANOMALY_AUDIO_CLIP` provenance and explicit fail-closed unavailability/failure states.
3. V2 Feishu decision-first projection and remote canonical read-back.
4. V2-specific hard release gate including Golden #002 and three PCAP E2E cases (normal/no-BYE, explicit RTP loss, protocol BYE termination).
5. Exact-head Source Manifest, Full Software Acceptance and Preliminary Evidence V1 compatibility PASS with the V2 gate PASS.
6. Merge to master, then production Shadow -> Canary -> Default V2 with Runtime Verify and Exact Source Binding retained.
7. Final production report/read-back acceptance and this implementation log changed to `CLOSED` only after all above evidence exists.

## Rules for this log

1. Record implementation wall-clock from the fixed start timestamp; do not reset after each PR.
2. Record each PR's Source Manifest, Full Acceptance, Preliminary Evidence and any new V2 gate wall time.
3. Record controlled-runner job duration separately from workflow wall time.
4. Flag any regression where V2 adds duplicated Frozen/Full/Golden work instead of reusing authoritative evidence.
5. Do not weaken Source Manifest, Full Acceptance, Runtime Verify, Exact Source Binding or existing frozen V1 gates to improve timing.

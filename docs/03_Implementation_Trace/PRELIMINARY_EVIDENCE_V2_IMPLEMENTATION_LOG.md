# Preliminary Evidence Report V2 Implementation Log

Status: IN PROGRESS

## Time tracking

- Implementation start: `2026-09-03T14:59:01+08:00`
- Timezone: `UTC+08:00`
- Start master SHA: `bd8569d7031385461ac4279c95b452e062d834d3`
- Implementation branch: `feat/evidence-v2-foundation`
- Documentation baseline PR: `#118`

This log is updated during implementation. Overall elapsed time is measured from the start timestamp above through final production acceptance. CI wall-clock and phase timings are recorded separately so CI/CD waiting is not confused with coding elapsed time.

## CI/CD Performance V2.2 baseline observation

Reference PR: `#116`, head `2eeefd91204475a6006b732112398a8348a93f08`.

Observed final successful PR run:

| Gate | Start | End | Wall time | Notes |
|---|---|---|---:|---|
| Source Manifest Gate | 06:05:40Z | 06:06:06Z | 0m26s | GitHub-hosted exact-SHA bundle/manifest path is fast. |
| Full Software Acceptance workflow | 06:05:40Z | 06:14:26Z | 8m46s | Includes pre-job/dependency/scheduling delay. |
| Full Software Acceptance self-hosted job | 06:09:23Z | 06:14:25Z | 5m02s | Actual controlled-runner critical work is now about five minutes. |
| Preliminary Evidence V1 Acceptance | 06:05:45Z | 06:14:34Z | 8m49s | Almost all time is waiting for exact-SHA Full Acceptance evidence; verification itself is seconds. |

Full Acceptance phase breakdown on `voip-controlled-linux-01`:

- Frozen PRD/SPEC contracts: ~37s
- Full VOIP AI software release gate: ~3m35s
- Golden #001: ~18s
- Human Evidence Gate: ~5s
- source download/materialization + setup + evidence upload: remainder

Initial conclusion: **V2.2 has materially reduced duplicated heavy work and the actual controlled-runner acceptance job is ~5 minutes, but end-to-end PR wall clock was still ~8m49s in the final PR #116 run because the Full Acceptance job did not start until ~3m43s after workflow creation.** This implementation will measure a fresh code PR to determine whether that delay was transient GitHub scheduling/dependency latency or a remaining systematic bottleneck.

## Implementation milestones

| Milestone | Status | Started | Completed | Elapsed | Notes |
|---|---|---|---|---|---|
| M0 V2 docs/CR baseline | DONE | before implementation | 2026-09-03 | - | PR #118 |
| M1 Golden #002 + Call/Timeline foundation | IN PROGRESS | 2026-09-03 14:59 +08 | - | - | First code PR |
| M2 Finding events/correlation/visibility | NOT STARTED | - | - | - | |
| M3 Artifact binding + semantic validator | NOT STARTED | - | - | - | |
| M4 Recommendation + Report UX V2 | NOT STARTED | - | - | - | |
| M5 Shadow/canary/full acceptance | NOT STARTED | - | - | - | |

## Rules for this log

1. Record implementation wall-clock from the fixed start timestamp; do not reset after each PR.
2. Record each PR's Source Manifest, Full Acceptance, Preliminary Evidence and any new V2 gate wall time.
3. Record controlled-runner job duration separately from workflow wall time.
4. Flag any regression where V2 adds duplicated Frozen/Full/Golden work instead of reusing authoritative evidence.
5. Do not weaken Source Manifest, Full Acceptance, Runtime Verify, Exact Source Binding or existing frozen V1 gates to improve timing.

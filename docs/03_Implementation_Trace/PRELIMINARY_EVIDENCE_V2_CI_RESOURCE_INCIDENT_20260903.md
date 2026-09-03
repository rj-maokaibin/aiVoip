# Preliminary Evidence V2 CI Resource Incident — 2026-09-03

Status: RECOVERY IN PROGRESS

## Scope

This checkpoint records the self-hosted runner resource incident encountered while validating PR #124. It is an implementation trace only and does not weaken or bypass any release gate.

## Evidence

- Runner: `voip-controlled-linux-01`
- Host: `srv`
- Host memory observed during incident: 23 GiB total, ~20 GiB used, ~2.5 GiB available, no swap.
- Kernel reported repeated `global_oom` events.
- Repeated OOM victims were VS Code Remote C/C++ `cpptools` processes, each reaching roughly 8.5–8.9 GiB anonymous RSS before being killed.
- The Full Software Acceptance run previously failed during the AI3 Case Copilot pytest phase with exit code 137 (`Killed`).
- VOIP AI service processes observed at the same time were materially smaller than the runaway `cpptools` processes; the incident is therefore treated as host resource contention rather than an Evidence V2 functional test failure.

## Recovery action

The runaway `cpptools` memory pressure was cleared on the host. The exact-head acceptance must be rerun after the runner reconnects. No stale-SHA success may be reused.

## Exact-head checkpoint

At `2026-09-03T18:52:45+08:00` (3h53m44s since the fixed implementation start `2026-09-03T14:59:01+08:00`):

- PR: #124
- Prior exact head before this checkpoint: `b7fb572e2ef0461ac711f01dd1f65f5861257fe7`
- Source Manifest Gate on that head: PASS
- Full Software Acceptance: queued because the self-hosted runner had not yet reclaimed the job
- Preliminary Evidence Acceptance: not acceptable as final evidence until the exact-head Full Acceptance succeeds
- Merge policy: do not merge until Source Manifest + Full Acceptance + Preliminary Evidence are successful for the same current PR head

This documentation commit intentionally creates a fresh user-authored exact head so GitHub Actions can re-evaluate the recovered runner against the current branch without relying on stale workflow state.

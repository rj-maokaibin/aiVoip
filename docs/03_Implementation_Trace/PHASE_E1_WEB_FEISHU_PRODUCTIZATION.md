# Phase E1 — Web Workbench + Feishu Card Contract

## Scope

Phase E1 continues productization while EC-02 real-device Platform Contract remains RESERVED.
It does **not** add or guess any DUT command. All autonomous reproduction actions still run only through the existing Mock Platform until EC-02 is explicitly closed.

## Implemented

### Web engineering workbench
- Case list + Case intake.
- Case workspace tabs: Overview / Diagnosis / Reproduction / Experiments & Causality / Evidence / Feishu Card / Audit.
- Case-scoped SSE refresh for diagnosis, reproduction, target capture, cleanup alerts, experiments, fix verification and evidence events.
- Diagnosis view for hypotheses, DiagnosticQuestion DAG and known/unknown/excluded decision state.
- Reproduction view for approved profiles, autonomous session creation, safe stop, capture health, Attempt/Call and CONTROL/TARGET verdicts.
- Experiment view for ExperimentProfile selection, run planning, minimal external-action acknowledgement, causal assessment and Fix Verification visibility.
- Evidence inventory exposes kind/scope/level/completeness rather than flattening unavailable and zero-value results.
- Existing Packet Intelligence and Media Intelligence are retained inside the workbench: SIP ladder, RTP/PCM correlation, periodic interference, active-media audio events, echo, WAV playback, waveform, spectrogram and unified timeline.
- Audit timeline is available directly in the Case workspace.
- Existing basic collection and diagnosis report generation remain available from the Case header.

### Read contracts added for workbench
- `GET /api/v1/cases/{case_id}/reproductions`
- `GET /api/v1/cases/{case_id}/experiments`
- `GET /api/v1/cases/{case_id}/fix-actions`
- `GET /api/v1/cases/{case_id}/fix-verifications`

### Feishu contract foundation
- Deterministic single-Case card builder.
- Card shows Case state, diagnosis level, reproduction state, CONTROL/TARGET counts, capture completeness, evidence sufficiency, cleanup and fix verification.
- Card wording maps hypothesis states to observed/support/strong-support/confirmed semantics and does not promote SUPPORTED to confirmed.
- Card buttons are contract values only: View Detail, Safe Stop Reproduction, and minimal External Action Completed when applicable.
- Frozen notification policy: routine Call/Attempt updates are silent; ARMED, TARGET, cleanup alerts, causal root-cause confirmation and final fix verification can notify.
- Preview endpoint: `GET /api/v1/cases/{case_id}/feishu/card-preview`.

## Explicitly not implemented in Phase E1

- No live Feishu tenant-token/webhook/API transport.
- No live Feishu callback endpoint/signature validation.
- No Feishu credentials are stored or guessed.
- No real DUT Voice VLAN/Gateway/Debug/Hook/PCM command is introduced.
- EC-02 remains `RESERVED / PENDING_PLATFORM_CONTRACT` and continues to block production autonomous reproduction.

Live Feishu transport should be bound only after deployment credentials, chat target and callback security configuration are supplied and reviewed.

## Contract safeguards

`tools/workbench_contract_gate.py` checks:
- required workbench tabs,
- SSE event coverage,
- safe-stop wording/behavior,
- EC-02 pending banner,
- Case read APIs for reproduction/experiment/fix,
- single Feishu card contract,
- absence of live Feishu transport in E1,
- absence of real DUT command strings in Web/Feishu code.

## Validation notes

Backend/unit and all existing M6.2 deterministic gates continue to pass. The environment does not provide Docker runtime, so real Compose runtime remains unverified. Frontend package installation could not complete in the isolated environment; TypeScript/TSX syntax was validated using the available TypeScript compiler API, while a full Vite production build remains a deployment/full-stack gate item.

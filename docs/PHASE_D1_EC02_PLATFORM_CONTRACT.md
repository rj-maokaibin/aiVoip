# Phase D1 — EC-02 Platform Contract Foundation

## Status

`PARTIAL / SAFE-READONLY-READY / AUTONOMOUS-REPRODUCTION-BLOCKED`

Phase D1 starts the real-device EC-02 work without guessing any unresolved DUT behavior. It extracts only source-backed commands from `voip 排障案例_思路整理。(1).md` and preserves previously confirmed PCM destination semantics. The resulting platform profile is intentionally **not production-ready for autonomous reproduction**.

## Implemented

- Added versioned `PlatformProfileDefinition` and `PlatformProfileRegistry`.
- Added explicit `ContractGap` and production-readiness evaluation.
- Added `RUIJIE_VOIP_AIM_V1@0.1.0` with checksum and PARTIAL status.
- Registered source-backed L0 read-only actions:
  - aimd process
  - VOIP adapter log
  - `/etc/config/network`
  - `/etc/config/vlan_ref`
  - `brctl show`
  - `route`
  - VOIP resolv.conf
  - networkvoip log
  - PCM counters
  - AIM bind interface
  - AIM SIP config/running state
  - DSP running state
- Upgraded Action Registry with duplicate detection and contract metadata.
- Added persistent AIM PTY root session. Root-level AIM commands reuse one PTY; prompt failure invalidates the session so the next command starts from a clean root state.
- Added `platform_contract_gate.py`:
  - audit mode passes and reports explicit gaps;
  - `--require-production-ready` intentionally blocks until EC-02 is complete.

## Explicitly NOT implemented

The following are **not guessed** and remain blockers:

1. exact Voice VLAN parser/field grammar;
2. exact Voice Gateway IP resolver command/path/field;
3. exact `br-lan_xx` UP-state verification parser;
4. PCM RX/TX OFF commands and their output semantics;
5. debug OFF commands and idempotency;
6. realtime OFFHOOK/ONHOOK event grammar and timestamp source;
7. FXS AIM submode prompt contract.

Known PCM start forms are stored only as `DOCUMENTED_ONLY` templates:

- `voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx on`
- `voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx on`

They are **not** registered as production-executable platform actions because cleanup has not been confirmed.

## Safety rule

`RUIJIE_VOIP_AIM_V1.autonomous_reproduction_actions` is empty. The Mock Platform remains the only executable M6.2 reproduction platform. A future Phase D2 may promote real actions only after each blocking gap is closed and the production platform gate passes.

## Validation

- Backend tests: 128/128 PASS
- M6.2 C1 Mock E2E: 3/3 PASS
- M6.2 C2 Evidence E2E: 5/5 PASS
- M6.2 C3 Experiment E2E: 4/4 PASS
- Reproduction profiles: 8/8 PASS
- Diagnostic questions: 17/17 PASS
- Experiment profiles: 6/6 PASS
- Rule validation: 11/11 PASS
- Synthetic Golden: 21/21 PASS
- Synthetic E2E: 53/53 PASS
- Baseline regression: 0 regressions / 0 observed changes
- APF1250 Field Golden: 15/15 PASS
- Platform contract audit gate: PASS (PARTIAL profile recognized)
- Platform production gate: BLOCKED by design (6 autonomous-reproduction blockers)

## Inputs required to complete Phase D2

Provide representative textual outputs / confirmed commands for:

1. `/etc/config/vlan_ref` and `/etc/config/network` with Voice VLAN configured;
2. command and output containing the Voice Gateway IP;
3. `brctl show` plus interface-state output for the active `br-lan_xx`;
4. exact `pcm_rx off` and `pcm_tx off` commands plus observed output;
5. exact debug disable commands for hook/SIP/IPC/DSP debug;
6. a short realtime debug transcript covering `onhook -> offhook -> onhook`;
7. interactive AIM transcript for `voip fxs 1` and `show information` if FXS snapshot support is desired.

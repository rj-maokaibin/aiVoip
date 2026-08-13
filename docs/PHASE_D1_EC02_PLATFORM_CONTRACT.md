# Phase D1 — EC-02 Platform Contract Foundation

## Status

`PARTIAL / RUNTIME-CONTEXT-VERIFIED / AUTONOMOUS-REPRODUCTION-BLOCKED`

Phase D1 starts the real-device EC-02 work without guessing any unresolved DUT behavior. The
2026-08-13 device transcripts additionally confirm the structured Voice Gateway and Voice VLAN
sources, dynamic `br-lan_<vlanid>` interface state, realtime FXS events, symmetric PCM cleanup and
debug-log cleanup. PCM OFF is explicitly non-idempotent: a second OFF exits the AIM CLI. The
platform profile is intentionally **not production-ready for autonomous reproduction** until
recovery performs verify-before-execute cleanup instead of blindly repeating OFF. The first live
RX experiment on 2026-08-13 showed that AIM accepts `pcm_rx on` while an idle UDP 40000 probe
remains quiet; quiet media alone is not proof that the diagnostic state is disabled.

## Implemented

- Added versioned `PlatformProfileDefinition` and `PlatformProfileRegistry`.
- Added explicit `ContractGap` and production-readiness evaluation.
- Updated `RUIJIE_VOIP_AIM_V1@0.5.1` with checksum and PARTIAL status.
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
- Added verified L0 `dev_config get -m voipServInfo` and `dev_config get -m voice_vlan` actions.
- Added strict JSON resolvers: exactly one `data` object with an IP-valued `svrName` yields the
  Voice Gateway; enabled `voice_vlan.vlanid` in `1..4094` yields the Voice VLAN.
- Added dynamic interface verification from `ip -o link show`: the resolver derives
  `br-lan_<voice_vlan_id>` and requires both `UP` and `LOWER_UP`; VLAN 400 is a sample value, not
  a product constant.
- Added the `AIM_FXS_EVENT_V1` parser for timestamped per-line `OFFHOOK`, `DTMF<digit>` and
  `ONHOOK` records.
- Recorded PCM RX/TX as `CONFIRMED_REVERSIBLE`; both OFF commands stop their corresponding UDP
  40000/50000 streams.
- Corrected both PCM cleanup contracts to `CONFIRMED_NON_IDEMPOTENT`: issuing either OFF for a
  second time exits the AIM command line.
- Added `VERIFY_QUIET_THEN_EXECUTE_ONCE`: skip OFF when the corresponding UDP stream is already
  quiet; execute OFF once only when the stream is active, then verify quiet.
- Added `app/reproduction/pcm_cleanup.py`: a transport-injected PCM cleanup guard with the
  APF1250 BusyBox `timeout -t`/`tcpdump` packet-count probe parser. Its persisted
  `off_already_executed` input prevents a watchdog retry from issuing a second PCM OFF.
- Bound the guard into `ReproductionOrchestrator`: `pcm_cleanup_guard` is injected as an optional
  transport dependency. When present, `cleanup()` routes `STOP_PCM_RX`/`STOP_PCM_TX` through the
  guard instead of a blind OFF:
  - a quiet UDP stream skips the non-idempotent OFF and is recorded `quiet_verified`;
  - an active stream executes exactly one OFF then re-probes;
  - a watchdog retry restores `off_already_executed` from the most recent completed `CleanupRun`
    and never issues a second OFF for a channel that already executed its only permitted one
    (`retry_blocked` keeps the session in `CLEANUP_FAILED` for operator investigation).
  Guard results are merged into the reverse-validation snapshot so `CleanupReadinessBarrier`
  still gates the session, and each channel's OFF history is persisted in
  `CleanupRun.action_results_json['pcm_guard']`.
- Confirmed the debug bundle is stopped by `debug p off`, `de p off` and
  `voip sip log-pkt off`. The other debug-level commands require no dedicated cleanup.
- Confirmed repeated `pcm_rx off` and `debug p off` execution. Retry safety for `pcm_tx off`,
  `de p off` and `voip sip log-pkt off` remains pending.
- Added `platform_contract_gate.py`:
  - audit mode passes and reports explicit gaps;
  - `--require-production-ready` intentionally blocks until EC-02 is complete.

## Explicitly NOT implemented

The following are **not guessed** and remain blockers:

1. validating the guard-bound sequence `ON -> active -> single OFF -> quiet` against the real
  platform adapter during a live call; the orchestrator binding is done and unit-tested with a
  transport-injected guard, but the real-device adapter that supplies the tcpdump probe and AIM
  executor is not yet wired into a live reproduction session;
2. proving that idle UDP quietness represents a disabled diagnostic state, or obtaining a
  read-only diagnostic-state query; the 2026-08-13 RX ON experiment showed quiet UDP while idle;
3. retry idempotency for `de p off` and `voip sip log-pkt off`;
4. FXS AIM submode prompt contract.

Known PCM start forms are stored as `CONFIRMED_REVERSIBLE` templates:

- `voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx on`
- `voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx on`

The confirmed debug enable sequence is also preserved as controlled templates: `debug p on`,
`debug sys debug`, `de p on`, `de sip de`, `de ipc de`, `de cm de`, `de dsp de`, `de sys de`,
and `voip sip log-pkt on`.

None of these commands are registered as production-executable platform actions until all cleanup
commands are confirmed retry-safe for crash recovery.

## Safety rule

`RUIJIE_VOIP_AIM_V1.autonomous_reproduction_actions` is empty. The Mock Platform remains the only executable M6.2 reproduction platform. A future Phase D2 may promote real actions only after each blocking gap is closed and the production platform gate passes.

## Validation

- PCM cleanup guard: 5/5 focused tests PASS (quiet skip, active single OFF, retry block, BusyBox
  zero-packet parsing, malformed probe rejection)
- PCM guard -> orchestrator binding: 3/3 focused tests PASS (quiet cleanup skips OFF,
  active cleanup executes each channel OFF once, watchdog retry restores
  `off_already_executed` and never sends a second OFF for a channel that already ran its only
  permitted OFF)
- Reproduction regression (C1/C2/C3 + binding + guard): 42/42 PASS
- Controlled DUT RX idle experiment, 2026-08-13: `pcm_rx on` was accepted at the AIM root prompt;
  UDP 40000 remained at zero captured packets during the idle 5-second probe. A single `pcm_rx off`
  then returned normally and the final idle probe also captured zero packets. This restores the
  test device but does not prove that OFF prevents PCM on a later active call.

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

1. provide or implement a non-mutating way to determine whether UDP 40000/50000 PCM forwarding is
  active before sending OFF; packet observation is acceptable if no DUT status command exists;
2. execute `de p off` twice and confirm logs remain stopped;
3. execute `voip sip log-pkt off` twice and confirm the second call is harmless;
4. interactive AIM transcript for `voip fxs 1` and `show information` if FXS snapshot support is desired.

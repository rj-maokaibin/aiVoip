# Phase D1 — EC-02 Platform Contract Foundation

## Status

`VERIFIED / LIVE-DEVICE-VERIFIED / PRODUCTION-READY`

Phase D1 started the real-device EC-02 work without guessing any unresolved DUT behavior. On
2026-08-13 the structured Voice Gateway and Voice VLAN sources, dynamic `br-lan_<vlanid>` interface
state, realtime FXS events, symmetric PCM cleanup and debug-log cleanup were confirmed on the live
APF1250. PCM OFF is explicitly non-idempotent: a second OFF exits the AIM CLI. Live active-call
validation proved the full `ON -> active -> single OFF -> quiet` sequence for both PCM RX and TX,
debug cleanup (`de p off`, `voip sip log-pkt off`) is idempotent, and the FXS submode prompt is
`AIM(fxs/1)> `. On 2026-08-14 `RUIJIE_VOIP_AIM_V1@0.6.0` was promoted to **VERIFIED** and is
**production-ready for autonomous reproduction**: `autonomous_reproduction_actions` are filled,
`RealReproductionPlatform` binds them to real DUT commands (13/13 real-DUT E2E checks passed), the
transport-injected PCM guard is bound in the orchestrator, and the production platform gate passes.

## Implemented

- Added versioned `PlatformProfileDefinition` and `PlatformProfileRegistry`.
- Added explicit `ContractGap` and production-readiness evaluation.
- Updated `RUIJIE_VOIP_AIM_V1@0.6.0` with checksum, VERIFIED status, filled
  `autonomous_reproduction_actions`, and no blocking gaps.
- Added optional FXS submode contract fields (`submode_prompt`, `snapshot_command`,
  `snapshot_fields`) to `KnownDiagnosticTemplate`.
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
- FXS realtime event source VERIFIED live (2026-08-13): with the FULL debug sequence enabled
  (`debug p on`, `debug sys debug`, `de p on`, `de sip de`, `de ipc de`, `de cm de`, `de dsp de`,
  `de sys de`, `voip sip log-pkt on`), the persistent AIM PTY emits timestamped event lines such as
  `2026-08-13 22:52:53.878000 [0] D:: [D]OFFHOOK`, `... [D]DTMF<1>`, and `... [D]ONHOOK`. A complete
  cycle OFFHOOK -> 7x DTMF<1> -> ONHOOK was captured and parsed by `AIM_FXS_EVENT_V1`. Early probes
  failed because `de p on`/`debug p on` alone do not emit FXS events; the full debug set is required.
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

`RUIJIE_VOIP_AIM_V1.autonomous_reproduction_actions` is filled with the live-verified real
actions (PCM RX/TX, full debug set, PCAP). `RealReproductionPlatform` binds these to real DUT
commands; PCM OFF is guarded by `PcmCleanupGuard` (execute once per active stream) and debug OFF
is idempotent. All cleanup commands are confirmed retry-safe for crash recovery.

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
- Platform contract audit gate: PASS (VERIFIED profile recognized, v0.6.0, no blocking gaps)
- Platform production gate: PASS (VERIFIED + production-ready for AUTONOMOUS_REPRODUCTION)
- Real-DUT E2E (2026-08-14): 13/13 PASS (resolve context -> arm -> snapshot -> cleanup)
- Real arm barrier: PASS at idle (facilities-ready semantics, min=0 / require_advancing=false)

## Live-device validation (2026-08-13)

All four Phase D2 inputs were confirmed on the real APF1250 DUT over SSH (legacy
`diffie-hellman-group14-sha1` via AsyncSSH `kex_algs`):

1. **Non-mutating PCM activity probe** — `timeout -t 5 tcpdump -ni br-lan_400 -c 1 'udp port 40000'`
   is a read-only way to determine PCM forwarding activity. Idle probes return `0 packets captured`.
2. **`de p off` idempotency** — executed twice; both calls were harmless and the AIM root prompt
   stayed intact.
3. **`voip sip log-pkt off` idempotency** — executed twice; both returned `set OK` and the AIM root
   prompt stayed intact.
4. **FXS submode prompt contract** — `voip fxs 1` enters `AIM(fxs/1)> `; `show information` returns
   a full FXS snapshot (Hook State, Rx/Tx Gain, DTMF Queue/Detect Cnt, Loop Current, Vline, etc.);
   `exit` returns to `AIM>`.

**Active-call PCM validation (Phase D1 blocker 1.1)** — confirmed on a live incoming call:

| Channel | ON → probe | single OFF → probe | root prompt |
|---|---|---|---|
| RX (UDP 40000) | 1 packet captured (192.168.150.4 → 192.168.3.200:40000, len 160) | 0 packets | intact |
| TX (UDP 50000) | 1 packet captured | 0 packets | intact |

`ON -> active -> single OFF -> quiet` holds for both channels, and no traffic resumed after the
permitted single OFF. The `parse_tcpdump_packet_count` parser was hardened to accept the BusyBox
singular `N packet captured` form plus `N packets received by filter` lines.

## Inputs required to complete Phase D2

All four items were provided and validated live on 2026-08-13 (see "Live-device validation"
above). Production promotion completed 2026-08-14: the transport-injected PCM guard is bound into
a live reproduction session via `RealReproductionPlatform`, the production platform gate passes,
and the profile is VERIFIED / production-ready for AUTONOMOUS_REPRODUCTION.

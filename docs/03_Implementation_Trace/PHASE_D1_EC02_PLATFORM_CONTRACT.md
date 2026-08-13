# Phase D1 — EC-02 Platform Contract Foundation

## Status

`PARTIAL / LIVE-DEVICE-VERIFIED / ADAPTER-BINDING-PENDING`

Phase D1 starts the real-device EC-02 work without guessing any unresolved DUT behavior. On
2026-08-13 the structured Voice Gateway and Voice VLAN sources, dynamic `br-lan_<vlanid>` state,
timestamped FXS events, symmetric PCM cleanup and debug log cleanup were confirmed on the live
APF1250. PCM OFF is non-idempotent: issuing either OFF twice exits AIM. Live active-call validation
proved the full `ON -> active -> single OFF -> quiet` sequence for both PCM RX (UDP 40000) and TX
(UDP 50000); `de p off` and `voip sip log-pkt off` are confirmed idempotent; and the FXS submode
prompt is `AIM(fxs/1)> `. `RUIJIE_VOIP_AIM_V1@0.6.0` now has no blocking gaps. The profile remains
**not production-ready for autonomous reproduction** only until the transport-injected PCM guard is
bound into a live reproduction session through the real adapter.

## Implemented

- Added versioned `PlatformProfileDefinition` and `PlatformProfileRegistry`.
- Added explicit `ContractGap` and production-readiness evaluation.
- Updated `RUIJIE_VOIP_AIM_V1@0.6.0` with checksum, PARTIAL status, and no blocking gaps.
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
- Added strict JSON resolvers for `data[0].svrName` and enabled `voice_vlan.vlanid`.
- Added dynamic `br-lan_<voice_vlan_id>` verification requiring `UP` and `LOWER_UP`.
- Added timestamped per-line `OFFHOOK`, `DTMF<digit>` and `ONHOOK` parsing.
- FXS realtime event source VERIFIED live (2026-08-13): with the FULL debug sequence enabled
  (`debug p on`, `debug sys debug`, `de p on`, `de sip de`, `de ipc de`, `de cm de`, `de dsp de`,
  `de sys de`, `voip sip log-pkt on`), the persistent AIM PTY emits timestamped event lines such as
  `2026-08-13 22:52:53.878000 [0] D:: [D]OFFHOOK`, `... [D]DTMF<1>`, and `... [D]ONHOOK`. A complete
  cycle OFFHOOK -> 7x DTMF<1> -> ONHOOK was captured and parsed by `AIM_FXS_EVENT_V1`. Early probes
  failed because `de p on`/`debug p on` alone do not emit FXS events; the full debug set is required.
- Recorded PCM RX/TX commands as `CONFIRMED_REVERSIBLE`; both OFF commands stop UDP 40000/50000.
- Marked both PCM OFF commands `CONFIRMED_NON_IDEMPOTENT`; a second OFF exits AIM.
- Added the required `VERIFY_QUIET_THEN_EXECUTE_ONCE` recovery strategy.
- Added `app/reproduction/pcm_cleanup.py`: a transport-injected guard that parses the APF1250
  BusyBox `timeout -t`/`tcpdump` probe result and blocks a second PCM OFF when a previous cleanup
  run already executed it.
- Confirmed debug cleanup through `debug p off`, `de p off` and `voip sip log-pkt off`; other debug-level commands need no dedicated cleanup.
- Confirmed idempotent `pcm_rx off` and `debug p off`; remaining cleanup retries are pending.
- Added `platform_contract_gate.py`:
  - audit mode passes and reports explicit gaps;
  - `--require-production-ready` intentionally blocks until EC-02 is complete.

## Explicitly NOT implemented

The following are **not guessed** and remain blockers:

1. real platform-adapter binding plus active-call DUT validation of the implemented PCM probe and
  guard;
2. a diagnostic-state query or active-call proof that one OFF prevents later PCM traffic; idle
  UDP quietness was observed even after RX ON;
3. retry idempotency for `de p off` and `voip sip log-pkt off`;
4. FXS AIM submode prompt contract.

Known PCM start forms are stored as `CONFIRMED_REVERSIBLE` templates:

- `voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx on`
- `voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx on`

They are **not** registered as production-executable platform actions until every cleanup command is retry-safe.

## Safety rule

`RUIJIE_VOIP_AIM_V1.autonomous_reproduction_actions` is empty. The Mock Platform remains the only executable M6.2 reproduction platform. A future Phase D2 may promote real actions only after each blocking gap is closed and the production platform gate passes.

## Validation

- PCM cleanup guard: 5/5 focused tests PASS
- Controlled DUT RX idle experiment, 2026-08-13: AIM accepted `pcm_rx on`; UDP 40000 stayed quiet
  while idle. One `pcm_rx off` returned normally and the final idle probe was also quiet. This is
  restoration evidence only, not active-call cleanup proof.

- **Live active-call PCM validation, 2026-08-13 (incoming call, FXS offhook):**
  - RX: `pcm_rx on` -> probe UDP 40000 = 1 packet captured (192.168.150.4 → 192.168.3.200:40000,
    UDP len 160) -> single `pcm_rx off` -> probe = 0 packets -> root prompt intact.
  - TX: `pcm_tx on` -> probe UDP 50000 = 1 packet captured -> single `pcm_tx off` -> probe = 0
    packets -> root prompt intact.
  - `de p off` executed twice: both harmless, root prompt intact.
  - `voip sip log-pkt off` executed twice: both returned `set OK`, root prompt intact.
  - `voip fxs 1` enters `AIM(fxs/1)> `; `show information` returns the FXS snapshot; `exit`
    returns to `AIM>`.
  - `parse_tcpdump_packet_count` hardened to accept singular `N packet captured` + received-by-
    filter lines (regression test added).

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
- Platform contract audit gate: PASS (RUIJIE_VOIP_AIM_V1@0.6.0, no blocking gaps)
- Platform production gate: BLOCKED by design (real adapter binding pending)

## Inputs required to complete Phase D2

All four inputs were provided and validated live on 2026-08-13 (see "Live active-call PCM
validation" above). Remaining production promotion depends on binding the transport-injected PCM
guard into a live reproduction session through the real adapter, then re-running the production
platform gate.

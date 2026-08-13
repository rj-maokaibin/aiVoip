# Phase C1 — M6.2 Reproduction Intelligence Mock Platform Core

## Status

**IMPLEMENTED / VALIDATED (Mock Platform Core)**

Phase C1 implements the deterministic, persistent core of M6.2 Reproduction Intelligence against a **Mock Platform only**. It intentionally does **not** implement the reserved EC-02 real-DUT command contract.

## Contract boundary

Phase C1 follows the frozen M6.2 SPEC and Engineering Contract. The following are hard boundaries:

- Only registered abstract Action IDs may be referenced by reproduction profiles.
- Mock actions use `executor: mock`; no shell/AIM command is invented.
- No real Voice Gateway resolver command is implemented.
- No real AIM debug ON/OFF command is implemented.
- No real OFFHOOK/ONHOOK event source is implemented.
- These remain `EC-02 RESERVED / PENDING_PLATFORM_CONTRACT`.

## Implemented scope

### Persistent reproduction domain

- `ReproductionSession`
- `ReproductionAttempt`
- `ReproductionCall`
- `ReproductionEventRecord`
- `ReproductionProfile` / `ReproductionProfileVersion`
- `VoiceRuntimeContextSnapshot`
- `ArmValidationResult`
- `CaptureChannelHealth`
- `DeviceDiagnosticLock`
- `CleanupRun`
- `DiagnosticQuestion`
- Alembic migration `0008_reproduction_core`

### Deterministic state machine

- Event-driven session transition service
- Autonomous start path
- AUTO_ARMING → ARMED → WATCHING
- Attempt / Call binding
- Target / Control classification
- Between-attempt enhancement
- Evidence-sufficiency decision
- External-action cleanup/release/re-arm path
- Stop request through cleanup rather than worker kill
- Terminal completion/failure states

### Mock Platform

The Mock Platform simulates the already-frozen platform semantics without encoding real DUT commands:

- Exactly one Voice VLAN
- `br-lan_<vlan>` interface derivation/validation
- Voice gateway and DUT voice IP runtime context
- Full-PCAP channel
- PCM RX UDP/40000
- PCM TX UDP/50000
- Debug channel health
- Capture-channel degradation/failure simulation
- Cleanup leak simulation

Default mock values are test-only and are not a real PlatformProfile.

### ARM readiness

`ArmReadinessBarrier` validates required channels using data-plane state rather than command acknowledgement:

- PCAP stream/data advancing
- PCM RX actual packet activity
- PCM TX actual packet activity
- Debug channel enabled/healthy
- Generic profile partial-capability downgrade where explicitly allowed

### Cleanup reverse validation

`CleanupReadinessBarrier` validates:

- PCM RX quiet/off
- PCM TX quiet/off
- Debug off
- PCAP stopped
- `CLEANUP_VERIFIED / CLEANUP_DEGRADED / CLEANUP_FAILED`

The device lock is not released when cleanup remains unverified.

### Multi-attempt / multi-call

- Invalid OFFHOOK→ONHOOK attempt may return to WATCHING
- Call may bind via later SIP anchor when earliest low-level anchor is missed
- `MATCH / NO_MATCH / INCONCLUSIVE`
- `TARGET / CONTROL / INCONCLUSIVE`
- Target evidence can complete a session when deterministic sufficiency is met

### Evidence sufficiency

Deterministic evaluator distinguishes:

- retry same capture
- enhance capture between attempts
- capture recovery
- external action required
- sufficient evidence

It does not use a free-form LLM confidence threshold as the confirmation gate.

### Recovery / lease

- Durable per-device diagnostic lock
- Lease and heartbeat
- Expired lease recovery to orphan/cleanup path
- Cleanup watchdog retry
- Case-level bounded session retry

### Capture ring contract

Metadata-level `SegmentedRingBuffer` implements:

- pre-trigger eviction
- earliest-anchor freeze
- preserve-after-freeze behavior

Actual PCAP/PCM segmented file I/O is deferred to a later Phase C increment.

### Reproduction profiles

Eight frozen profiles are registered:

1. `REGISTER_FAILURE`
2. `CALL_SETUP_FAILURE`
3. `ONE_WAY_AUDIO`
4. `AUDIO_STUTTER`
5. `AUDIO_NOISE`
6. `DTMF_LOSS`
7. `ECHO`
8. `VOIP_GENERIC_FULL_CAPTURE`

Profiles reference only registered abstract actions and are validated for action safety and start/cleanup symmetry.

### API / worker seam

REST endpoints expose profile/session/attempt/call/evidence-bundle operations. Creation enqueues autonomous start. Celery reproduction tasks and a dedicated reproduction queue are present.

## Explicitly not complete in C1

The following remain for later Phase C increments and are **not** silently treated as complete:

- Real segmented PCAP/PCM/log file streaming and freeze/finalize into MinIO
- Integration of existing SIP/RTP/PCM analyzers into true `LIVE` and `CALL_QUICK` runtime modes (C1 uses deterministic mock quick-analysis seam)
- Full DiagnosticQuestion registry/DAG and all question-specific required-evidence contracts
- Full ExperimentProfile registry and A/B / A-B-A execution
- EnvironmentComparator
- Root-cause causal experiment confirmation implementation
- Full Fix Verification workflow and before/after comparison object
- Production-grade recovery against real SSH/PTY/capture processes
- Feishu reproduction card and Web reproduction timeline UI
- EC-02 real DUT PlatformProfile / Action mapping

## Validation

Validated on 2026-08-13:

- Backend tests: **106/106 PASS**
- Reproduction profile gate: **8/8 PASS**
- Reproduction mock E2E: **3/3 PASS**
- Rule validation: **11/11 PASS**
- Synthetic Golden: **5/5 cases, 21/21 assertions PASS**
- Synthetic E2E: **10/10 cases, 53/53 assertions PASS**
- Baseline regression: **0 regressions / 0 changes**
- APF1250 field Golden: **15/15 checks PASS**
- Python compile: **PASS**
- Docker Compose YAML parse: **PASS** (`docker-compose.yml` 11 services; `docker-compose.e2e.yml` 7 services)
- Real Docker runtime: **UNVERIFIED** because Docker CLI/daemon is unavailable in the current execution environment.

## Next increment

Recommended Phase C2:

1. Real collector-side segmented capture artifact pipeline using MockPlatform as the device-facing seam.
2. Evidence finalization + immutable artifact lineage + MinIO staging/finalize contract.
3. Existing deterministic packet/media analyzers exposed as `LIVE`/`CALL_QUICK` modes.
4. Expanded DiagnosticQuestion and evidence-sufficiency bindings.
5. Additional crash/recovery and negative-control tests.

EC-02 must remain untouched until the real platform contract is explicitly supplied and approved.

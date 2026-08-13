# Phase C2 — M6.2 Reproduction Evidence Capture Pipeline

## 1. Scope

Phase C2 converts the Phase C1 reproduction ring from metadata-only bookkeeping into a real file-backed evidence pipeline while retaining the **Mock Platform** boundary. It implements deterministic capture files, immutable evidence lineage, session finalization, and actual analyzer consumption of generated capture data.

This phase does **not** implement EC-02 real-DUT commands. No Voice VLAN/Gateway resolver command, AIM debug ON/OFF command, PCM control command, OFFHOOK/ONHOOK log source, or other DUT-specific command mapping is guessed in C2.

## 2. Evidence path implemented

The C2 path is:

```text
Mock Platform packet/log producer
        ↓
File-backed segmented ring
        ↓
Earliest Anchor → Freeze
        ↓
Retained RAW segment Evidence
        ↓
Call-scoped PCAP merge
        ↓
LIVE / CALL_QUICK deterministic analyzers
        ↓
Derived Finding Evidence + AnalyzerRun lineage
        ↓
Session Finalizer
        ↓
Session PCAP / Debug Evidence + evidence_manifest.json + SHA256
```

The PCAP is the canonical network/audio capture clock and includes SIP/RTP plus diagnostic PCM UDP traffic on the configured mock 40000/50000 destinations. PCM semantic analysis is derived from those captured UDP packets; C2 does not invent a separate real-device PCM receiver.

## 3. Capture storage model

Phase C2 adds the following persistent objects:

- `ReproductionCaptureState`
  - pre-trigger window
  - segment duration
  - preserve/freeze state
  - total bytes
  - finalization flag and manifest snapshot
- `ReproductionCaptureSegment`
  - session/attempt/call scope
  - channel and segment number
  - time range
  - local path/content type
  - SHA256/size
  - frozen/retained/status
  - retention class
  - immutable Evidence reference
- `EvidenceFinalizeRun`
  - idempotent session finalization run
  - final evidence IDs
  - manifest object key/SHA256
  - terminal error details

Migration: `0009_reproduction_evidence_capture.py`.

## 4. Segmented Ring semantics

Before an activity anchor, segments are `TEMP_RING` and may be physically evicted when outside the configured pre-trigger window.

On the earliest anchor:

1. `preserve_mode` is enabled.
2. eligible pre-trigger segments are frozen.
3. frozen segments are persisted as immutable `RAW` Evidence.
4. subsequent segments are immediately retained while the attempt is active.
5. an invalid attempt can be classified as `SHORT_ATTEMPT` without pretending it was a valid Call.

Retained Evidence is never deleted by ring eviction.

## 5. Immutable storage and lineage

For Mock/dev execution C2 provides `FilesystemObjectStorage`, which preserves object keys and immutable file semantics without requiring MinIO in unit tests. The production MinIO integration remains available through the storage factory.

Each retained raw segment records:

- source scope
- segment time range
- content SHA256
- capture pipeline version
- session/attempt/call IDs
- retention class

Call-level merged PCAPs are `DERIVED` Evidence with explicit `parent_evidence_ids` referencing the raw segments used to construct them.

Session finalization produces session-level derived PCAP/debug objects and `evidence_manifest.json`. Finalization is idempotent: a successful `EvidenceFinalizeRun` is reused instead of creating duplicate terminal artifacts.

## 6. Real analyzer integration in Mock Platform

C1 used `QuickAnalysisInput` as a deterministic mock verdict. In C2 it is only the Mock Platform's scenario injection/ground-truth input used to generate packet/audio data. The semantic findings themselves are produced by the existing analyzers from real generated PCAP bytes.

### LIVE mode

`LiveReproductionAnalyzer` runs during Call binding and produces lightweight evidence such as:

- `SIP_CALL_LIVE`
- `RTP_BASIC_LIVE`
- `PCM_STREAM_HEALTH`

Its `AnalyzerRun` is versioned with `mode=LIVE` and references the actual raw capture Evidence.

### CALL_QUICK mode

`EvidenceBackedCallQuickAnalyzer` consumes the merged Call PCAP and invokes the existing media intelligence engine with the C2 mock PCAP adapter and verified PCM profile. It can derive:

- SIP call attempt/classification
- active media window
- media direction
- SIP call failure
- one-way RTP media
- RTP burst loss
- periodic PCM interference
- PCM↔RTP correlation
- echo path
- DTMF path

The resulting `CALL_QUICK_FINDINGS` object is derived Evidence and is linked to an `AnalyzerRun` containing input/output Evidence IDs, analyzer mode, version, and configuration snapshot.

## 7. Mock PCAP codec and signal fixtures

C2 adds a narrow deterministic classic-PCAP codec for test-only Mock Platform execution. It supports Ethernet + IPv4 + UDP and enough SIP/RTP parsing to feed the existing engines without requiring TShark.

The Mock capture generator creates repeatable scenarios for:

- normal/control Call
- periodic audio interference
- RTP burst loss
- one-way RTP
- echo path
- DTMF path
- call setup failure fixtures

The fixture is not a replacement for EC-02 and is not used as a real-DUT command source.

## 8. Multi-Call contamination protection

Call post-capture windows can overlap a subsequent Call. C2 therefore binds self-contained Mock final Call segments by `call_id` before `CALL_QUICK` analysis. This prevents a previous control Call from contaminating the next target Call when their retention windows overlap.

## 9. C2 gates

New commands:

```bash
make reproduction-evidence-e2e
make m62-c2-gate
```

`m62-c2-gate` executes:

- Reproduction Profile Gate
- complete backend tests
- C1 reproduction E2E regression
- C2 evidence E2E
- Rule validation
- synthetic Golden replay
- synthetic E2E + baseline diff

## 10. Validation result

Phase C2 validation on 2026-08-13:

- Backend tests: **114/114 PASS**
- Reproduction Profiles: **8/8 PASS**
- C1 reproduction E2E regression: **3/3 PASS**
- C2 evidence E2E: **5/5 PASS**
- Rules: **11/11 PASS**
- Synthetic Golden: **5/5 cases, 21/21 checks PASS**
- Synthetic E2E: **10/10 cases, 53/53 checks PASS**
- Baseline regression: **0 regressions / 0 changes**
- APF1250 field Golden: **15/15 checks PASS** using the matching field PCAP SHA256
- Python compile: **PASS**
- Compose YAML parse: **PASS**
- Docker full-stack runtime: **UNVERIFIED** because the execution environment has no Docker CLI/daemon

## 11. Explicitly deferred after C2

C2 does not claim the following are complete:

- EC-02 real DUT PlatformProfile/Action mapping
- real SSH/tcpdump streaming transport and real DUT log readers
- production-time physical PCM/debug commands
- complete DiagnosticQuestion DAG
- ExperimentProfile execution
- A/B and A-B-A experiment orchestration
- EnvironmentComparator
- causal Root Cause confirmation gate implementation
- Fix Verification end-to-end
- Web/Feishu reproduction visualization and interaction
- real Docker/Compose full-stack runtime verification

These are later Phase C/D/E items and must not be silently inferred from C2.

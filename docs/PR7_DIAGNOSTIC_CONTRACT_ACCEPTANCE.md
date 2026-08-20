# PR7 DiagnosticEvent / CandidateDecision / Finding Contract Acceptance

## 1. Scope

PR7 unifies the diagnostic fact contract consumed by Evidence Report, Golden replay and future AI explanation layers.

Canonical flow:

```text
Analyzer V1 outputs
  -> compatibility projection
DiagnosticEvent v1
  -> deterministic disposition
CandidateDecision v2
  -> only ACCEPT is Finding-eligible
EvidenceFinding
  -> Finding Diagnostic Link v1
Canonical Report / Golden / AI
```

PR7 does **not** add new fault detectors, change acquisition orchestration, increase Evidence Level, or grant Root Cause authority.

## 2. Canonical contracts

### DiagnosticEvent v1

Every diagnostic fact has a deterministic `event_id` and carries:

- event type;
- analyzer id/version/profile provenance;
- Case/Session/Call/Stream/Tap scope;
- time range and time base;
- measurements and thresholds;
- context and negative conditions;
- Evidence / packet references;
- quality metadata;
- source reference.

Event identity must remain stable under harmless list reordering. Plain collection indexes are traceability metadata, not identity. Stable source keys such as candidate id, frame/sequence or stable key may participate in identity.

### CandidateDecision v2

Canonical dispositions are:

- `ACCEPT`: deterministic evidence is sufficient for Finding eligibility;
- `SUPPRESS`: deterministic negative control / normal transient matched;
- `INCONCLUSIVE`: suspicious candidate exists but evidence is insufficient or weak;
- `MERGE`: valid secondary event belongs to an already accepted stable Finding.

Hard invariant:

> A user-visible Finding MUST have at least one canonical `ACCEPT` decision.

`SUPPRESS` and `INCONCLUSIVE` remain auditable but MUST NOT justify a user-visible Finding.

### Finding Diagnostic Link v1

A Finding records stable references to:

- all source Event ids;
- all CandidateDecision ids;
- accepted Event ids;
- merged Event ids;
- suppressed/inconclusive ids when applicable to the local link contract.

Repeated valid events are not discarded. They keep individual Event identities and explicit `MERGE` decisions targeting the primary Event.

## 3. Legacy compatibility

Existing Media CandidateDecision V1 outputs remain unchanged in PR7 so existing Analyzer and regression contracts are not broken.

Compatibility mapping:

| Legacy status | Canonical status |
|---|---|
| `PROMOTED` | `ACCEPT` |
| `REJECTED_NEGATIVE_CONTROL` | `SUPPRESS` |
| `INCONCLUSIVE` | `INCONCLUSIVE` |

The adapter preserves legacy status, candidate id, reason code, negative controls and positive/activity evidence. It must not improve severity, Evidence Level or Root Cause authority.

Future Analyzers may emit the canonical contract directly; the adapter can then become a no-op/compatibility reader.

## 4. Analyzer fact projection

PR7 projects existing deterministic outputs into canonical Events:

- Packet Analyzer anomalies;
- PCM gap events;
- PCM periodic low-frequency / mains-family observations;
- PCM DTMF quality events;
- Media CandidateDecision events;
- Media cross-layer events not already represented by a CandidateDecision.

A direct Analyzer anomaly is projected as `ACCEPT` only because the current Analyzer already exposes it as a formal anomaly. PR7 does not independently redetect or strengthen the anomaly.

## 5. Direction identity rule

Direction vocabulary is not assumed to be globally identical.

Examples:

- endpoint direction: `src_ip:port->dst_ip:port`;
- role direction: `DUT_TO_PBX`, `PBX_TO_DUT`, `UPSTREAM`, `DOWNSTREAM`;
- PCM local direction: `RX`, `TX`.

Only directly comparable direction forms participate in Event/Finding identity matching. Role-only labels are retained as descriptive extension metadata and cannot alone cause an Event/Finding mismatch.

Call id, Stream id, Tap and SSRC are stronger matching keys.

## 6. Snapshot transport and persistence

The Analyzer-to-Report in-memory snapshot is transported through a private Analyzer-state extension so it is present even for packet-only reports. The private transport key is removed before publication.

Final persistence policy:

```text
PreliminaryEvidenceReport.snapshot_json
  diagnostic_contract
    events[]
    candidate_decisions[]
    summary

EvidenceFinding.correlation_json
  diagnostic_contract
    event_ids[]
    decision_ids[]
    accepted_event_ids[]
    merged_event_ids[]
```

The complete canonical Event/Decision objects are authoritative in the report snapshot. EvidenceFinding stores compact stable references through the existing JSON column; PR7 intentionally does not introduce a database migration.

## 7. Compatibility fallback

If an existing Finding cannot be uniquely matched to an Analyzer Event, PR7 creates an explicit compatibility Event with:

- analyzer id `finding_composer_adapter`;
- decision reason `LEGACY_FINDING_SOURCE_ADAPTED`;
- no Evidence Level or Root Cause upgrade.

Fallback is observable through `diagnostic_contract.summary.finding_fallback_event_count`.

Fallback is not a success signal. For P0 Analyzer families covered by direct projection, the target is to drive fallback count to zero in Golden replay.

## 8. Composer and idempotency

`REPORT_COMPOSER_VERSION` is bumped to `evidence-brief-composer-v4`.

PR7 reports must not reuse PR6/v3 idempotent report snapshots because the Canonical Report now contains `diagnostic_contract` plus Finding-level diagnostic links.

## 9. P0 software acceptance

PR7 software gate must verify all of the following:

1. DiagnosticEvent IDs are deterministic.
2. Harmless Analyzer list reordering does not change Event identity.
3. Stable source identity changes do change Event identity when appropriate.
4. CandidateDecision IDs are deterministic.
5. V1 legacy candidate statuses map losslessly to canonical dispositions.
6. `SUPPRESS` cannot justify a Finding.
7. `INCONCLUSIVE` cannot justify a Finding.
8. Every user-visible Finding has at least one `ACCEPT` decision.
9. Multiple accepted same-Finding events retain source identities and gain explicit `MERGE` decisions.
10. Packet-only reports preserve real Packet Analyzer Events without forced Finding fallback.
11. Private snapshot transport does not leak into published Analyzer summaries.
12. Full diagnostic snapshot is included in the Canonical Report.
13. Finding compact diagnostic references resolve to the report snapshot.
14. Contract projection never upgrades severity, Evidence Level or Root Cause authority.
15. Existing CandidateDecision V1 negative-control behavior remains compatible.

The dedicated Release Gate key is `DIAGNOSTIC_CONTRACT`.

## 10. Offline Golden acceptance

Offline Golden #001 must use the same canonical contract as runtime report generation.

For the current imported real PCAP case, the later controlled replay should verify at minimum:

- reconstructed target Call events retain stable Event ids across deterministic replay;
- two upstream HIGH_DELTA observations remain two Events but one stable Finding with explicit merge semantics;
- Sequence-continuous HIGH_DELTA does not become Packet Loss;
- DTMF/Click negative-control candidates remain auditable and suppressed candidates do not enter Findings;
- periodic PCM/cross-layer observations remain traceable without physical-root-cause overclaim;
- P0 directly supported findings do not silently fall back to `finding_composer_adapter`.

This remains **Offline Analysis Golden E2E**, not Live Acquisition Golden E2E.

## 11. Deferred environment gates

Per the stacked PR execution decision, PR7 remains Draft until the controlled environment executes the accumulated gates:

- `tools/voip_ai_release_gate.sh` / Evidence Report Release Gate;
- real Offline Golden #001 fixture replay;
- Live Feishu Tenant rendering/permission verification;
- real DUT acquisition E2E.

Until then PR7 status is:

```text
IMPLEMENTED / STATIC-REVIEWED / UNVERIFIED
```

It must not be represented as production PASS or merged solely from static implementation status.

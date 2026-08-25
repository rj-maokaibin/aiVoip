# PR6 Report Grounding / Explainability Hard Gate

PR6 does not create new Analyzer truth. It validates whether the final Canonical Report faithfully represents already-produced Analyzer/Finding/Artifact facts.

## Runtime publication boundary

The hard validator runs only after Finding Artifact refs and Evidence Cards exist and before final HTML/JSON publication.

- `BLOCKER` => `REPORT_GROUNDING_FAILED`; do not publish the report as a reviewable preliminary evidence report.
- `WARNING` => publication may continue, but reviewability must be `PARTIALLY_REVIEWABLE`.
- no issues => `FULLY_REVIEWABLE` when evidence completeness is also COMPLETE.

Offline replay/in-memory Findings are not allowed to prove live Acquisition Reliability.

## Validation layers

### Structural

- bound/reconstructed Call cannot disappear from `display_call`;
- Offline Imported report cannot bind a Reproduction Session;
- every Finding Artifact ref must exist in the Canonical Artifact inventory at publication time;
- every deterministic Claim must point to an existing Finding.

### Semantic

- HIGH_DELTA with continuous RTP Sequence and zero loss must be `DELAY_NOT_PACKET_LOSS`;
- Packet Loss/Burst Loss requires positive Sequence/Loss evidence;
- periodic/50-60Hz evidence must keep the physical Root Cause preliminary;
- an OBSERVED first-boundary claim requires a concrete first observable layer.

### Evidence

- MEDIUM/HIGH/CRITICAL Findings require an Evidence Card;
- RTP/audio quality Findings require a Finding-scoped primary visual where defined;
- visual artifacts must be report-safe and pass Renderer annotation contract;
- audio-required Findings must be AVAILABLE with report-safe anomaly clips, or explicit UNAVAILABLE reason;
- RTP packet anomaly Findings require Frame/Seq drill-down.

### Explainability

MEDIUM/HIGH/CRITICAL Findings require:

- what happened;
- root-cause boundary / unknowns;
- deterministic next action;
- human-readable time when available.

A/B conclusions may not be emitted without A/B comparison data.

## Deterministic Claim Manifest

Each Finding generates a Claim record containing only existing structured truth:

- claim ID/type/statement;
- Finding ref;
- scope;
- selected structured metrics;
- event refs;
- Frame/Seq refs;
- Artifact IDs;
- rule ID.

The Claim layer is for traceability/explanation. It must never upgrade Evidence Level or create a Root Cause conclusion.

## Required negative-control regressions

At minimum CI must block:

1. `Call count > 0 + reconstructed Call bound + display_call=null`;
2. Offline Imported + Reproduction Session;
3. Sequence continuous HIGH_DELTA labelled as Packet Loss;
4. Packet Loss Finding with zero/no loss evidence;
5. periodic Finding that explicitly confirms power/grounding/phone/SLIC root cause;
6. missing Artifact refs;
7. missing required visual or Frame/Seq drill-down;
8. `audio_status=AVAILABLE` pointing to raw/full WAV;
9. required audio marked UNAVAILABLE without a reason;
10. missing Finding boundary/next-action fields.

## Deferred controlled-environment gates

Per project decision, these are recorded but run later:

- full Linux `tools/voip_ai_release_gate.sh`;
- real Offline Golden #001 (`tcpdump-2026-08-14(2).pcap`);
- Live Feishu Tenant media projection;
- real DUT acquisition E2E.

PR6 implementation and static/unit regression can proceed before those environment gates, but the stacked PRs must remain Draft/UNVERIFIED until they are executed.

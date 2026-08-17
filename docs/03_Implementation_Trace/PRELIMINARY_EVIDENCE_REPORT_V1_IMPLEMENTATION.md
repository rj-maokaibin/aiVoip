# Preliminary Evidence Report V1 Implementation Trace

Status: IMPLEMENTATION / CI VALIDATION
Baseline: `docs/02_Core_Documents/Preliminary_Evidence_Report_V1.0/`

## Implemented in this branch

- Canonical `preliminary-evidence-report-v1` contract and report/finding state enums.
- Persistent Call / Session / Case report versions, stable Findings, report-artifact links, and one-Case-one-Feishu-document binding.
- Additive PCM metrics: RMS dBFS and Peak dBFS; explicit boundary that dBFS is not dB SPL.
- Deterministic Finding Composer for Packet / PCM / Media evidence.
- Deterministic first-observable-layer Evidence Boundary with UNKNOWN on missing upstream/control evidence.
- Call / Session / Case report composition and idempotent versioning; forced rebuild produces a distinct version key.
- Session/Case cross-Call aggregation by Finding Signature; Case aggregation is partitioned by Environment Fingerprint.
- A/B Finding reproduction-rate comparison with repeatability + absolute-difference V1 rule and explicit non-causal boundary.
- Deterministic PNG renderer for waveform, spectrogram, spectrum, RTP timeline and SIP flow artifacts.
- Reuse/link Media Analyzer audio WAV/Clip artifacts; materialize Packet/PCM/Media Analyzer JSON as report artifacts.
- Canonical JSON + standalone HTML + Manifest + INTERNAL_FULL / SHARE_SAFE Evidence Bundle.
- Read/rebuild/findings/artifacts/links/bundle APIs.
- Analyzer-completion debounce worker; automatically builds Call -> Session -> Case reports and queues Feishu projection.
- Feishu Docx projection using D112 ordering. Key images and abnormal audio clips are inserted in the Evidence Bundle/附件 section; latest report version is inserted at the top of the Case document.
- Report generation, bundle generation and Feishu projection actions are audited.

## Authority invariant

The preliminary report is evidence-only. It cannot elevate evidence level or independently confirm Root Cause. Historical cases and AI explanations remain non-authoritative for current-case root-cause confirmation.

## Current validation

Focused contract tests are included for dBFS semantics, first-observable boundary, D112 ordering, idempotency, A/B repeatability, deterministic PNG output, Feishu ordering, model registration and Root Cause Authority. Full repository CI is the next gate.

## Not claimed by this branch

- Real-DUT V1 release acceptance has not been executed by this branch.
- Golden Dataset precision/recall and Wrong Boundary Rate release metrics have not yet been proven.
- Feishu live tenant permissions and document projection require the configured live environment for final acceptance.

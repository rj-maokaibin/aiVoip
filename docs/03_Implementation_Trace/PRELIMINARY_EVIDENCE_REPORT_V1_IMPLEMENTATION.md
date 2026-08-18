# Preliminary Evidence Report V1.0 Implementation Trace

- Baseline: `docs/02_Core_Documents/Preliminary_Evidence_Report_V1.0/`
- Scope: Preliminary Evidence Report（初步证据分析报告）软件实现
- Implementation state: **SOFTWARE IMPLEMENTATION COMPLETE / CI RELEASE GATE**
- Root Cause Authority: **UNCHANGED**
- Production acceptance: **only 3 environment gates may remain pending**

## 1. Software capability trace

| Capability | Implementation | Verification |
|---|---|---|
| Call / Session / Case Canonical Report | `app/services/evidence_report.py`, `app/reports/evidence_brief.py` | `test_preliminary_evidence_report_v1.py` |
| Independent report schema/version | `preliminary-evidence-report-v1`, report persistence | schema/status/idempotency contract tests |
| Finding stable ID / Signature / lifecycle | `app/db/evidence_report_models.py`, `finding_composer.py` | finding model/signature/status tests |
| Packet/SIP/RTP Finding | `finding_composer.py` | report + packet tests |
| RTP Frame-level evidence | `analyzers/packet/rtp.py` | `test_rtp_frame_evidence_v1.py` |
| PCM RX/TX evidence | existing PCM Analyzer + report composer | full backend regression |
| RMS dBFS + Peak dBFS | `analyzers/pcm/signal.py` | report contract tests |
| dBFS ≠ dB SPL boundary | `signal.py`, report content | authority/non-goal tests |
| Silence / Click-Pop / low-frequency interference | PCM/Media Analyzer + Finding Composer | report/golden regression |
| Echo path evidence | Media Analyzer + Finding Composer | report/golden regression |
| DTMF signal quality anomaly | `analyzers/pcm/dtmf_quality.py`, PCM Analyzer, Finding Composer | `test_dtmf_finding_v1.py` + Golden gate |
| DTMF thresholds governed by versioned Profile | `profiles/analyzers/voip_v1.yaml` v1.1.0 + `analyzers/profile.py` validation | DTMF P0/profile contract test |
| DTMF evidence boundary | only low-confidence / short inter-digit timing facts; never invents a missing dialed digit without authoritative comparison | `test_dtmf_finding_v1.py` |
| First observable layer | `services/evidence_boundary.py` | authority/boundary tests |
| Missing upstream evidence → UNKNOWN | deterministic boundary implementation | boundary safety tests |
| Cross-Call aggregation/reproduction rate | `evidence_report_aggregation.py` | D112/rate tests |
| Environment Fingerprint isolation | `evidence_report_aggregation.py` | aggregate contract tests |
| A/B comparison | `evidence_report_aggregation.py` | A/B baseline/rate tests |
| Deterministic PNG evidence | `evidence_visuals.py` | renderer tests + performance gate |
| Waveform / spectrum / spectrogram | renderer + Media artifacts | artifact tests |
| RTP Timeline / SIP Call Flow | renderer | artifact tests |
| Abnormal audio clip | existing Media Artifact reused by report | source-artifact linkage tests |
| Full audio access | permission-controlled existing WAV Artifact | Artifact capability access |
| Analyzer JSON Artifact | `evidence_report_analysis_artifacts.py` | bundle/artifact tests |
| Manifest + SHA256 | `evidence_report_artifacts.py` | bundle contract tests |
| INTERNAL_FULL Bundle | `evidence_report_artifacts.py` | bundle profile tests |
| SHARE_SAFE excludes raw/full WAV | `evidence_report_artifacts.py` | `test_evidence_bundle_profile_v1.py` |
| Analyzer terminal → Report debounce | `workers/evidence_report_tasks.py` | worker/full regression |
| Feature flag rollout | `PRELIMINARY_EVIDENCE_REPORT_ENABLED` | config + worker/API guards |
| Feishu one-Case-one-document projection | `integrations/feishu/evidence_document.py` | Feishu contract test |
| Feishu D112 ordering | same | `test_feishu_evidence_document_v1.py` |
| Feishu mutable Case card summary | `integrations/feishu/cards.py` | full regression; live Tenant remains environment gate |
| Web R&D drill-down | `frontend/evidence-report.html`, `src/evidence_report_page.tsx` | frontend production build gate |
| Web Finding / Frame / PNG / audio / Bundle | same | TypeScript/Vite production build + backend contract |
| Evidence permissions | `api/evidence_permissions.py` | `test_evidence_permissions_v1.py` |
| VIEW_REPORT | capability policy | permission test |
| VIEW_RAW_EVIDENCE | capability policy + Artifact filtering | permission test |
| DOWNLOAD_EVIDENCE_BUNDLE | capability policy + bundle API | permission/bundle test |
| REBUILD_REPORT | capability policy | permission/API regression |
| MANAGE_RETENTION | reviewer/admin/service only | permission test |
| Raw PCAP/WAV 90-day retention | `services/evidence_retention.py` | `test_evidence_retention_v1.py` |
| Golden/manual-lock retention exemption | same | retention tests |
| Late Golden promotion rechecked before expiry | retention sweep refreshes stale STANDARD_90D rows before deletion | late-Golden retention test |
| Delete Payload, retain provenance metadata | same | retention expiry tests |
| Expired raw Evidence shown as unavailable | `evidence_report_scope.py`, `evidence_report.py` | `test_evidence_report_retention_expiry_v1.py` |
| Automatic report refresh after expiry | `workers/evidence_retention_tasks.py` | worker/full regression |
| Hourly retention sweep | Celery Beat | config/worker contract |
| Report Pipeline observability | `services/evidence_report_metrics.py` | `test_evidence_report_metrics_v1.py` |
| P50/P95 report service latency | metrics API | metrics test |
| Analyzer/Artifact/Feishu/queue status | metrics API | metrics test |
| Golden Dataset framework | `tools/evidence_report_golden_gate.py` | CI software release gate |
| Answer Leakage protection | Golden gate input/expected separation | Golden gate |
| Recall / Precision metrics | Golden gate | final real values remain environment gate |
| Per-P0 type Recall/Precision + serious false-positive gate | Golden gate | CI software release gate |
| Boundary correctness / wrong-boundary metrics | Golden gate | final real values remain environment gate |
| Software-core performance benchmark | `tools/evidence_report_performance_gate.py` | CI software release gate |
| Strict software Release Gate | `tools/evidence_report_release_gate.py` | CI |
| Full backend regression | `.github/workflows/ai-e1-e6.yml` | PostgreSQL + Redis + pytest |
| Frontend production build | same workflow | Node 22 + `npm ci` + `npm run build` |
| Alembic migrations | `0019`, `0020` | CI `alembic upgrade head` |
| Root Cause Authority invariant | report has no root-cause authority mutation | authority tests |

## 2. Evidence permission contract

| Role | Report | Raw Evidence | Bundle | Rebuild | Retention |
|---|---:|---:|---:|---:|---:|
| VIEWER | ✅ | ❌ | ❌ | ❌ | ❌ |
| ENGINEER | ✅ | ✅ | ✅ | ✅ | ❌ |
| EXPERT_REVIEWER | ✅ | ✅ | ✅ | ✅ | ✅ |
| ADMIN | ✅ | ✅ | ✅ | ✅ | ✅ |
| SERVICE | ✅ | ✅ | ✅ | ✅ | ✅ |

Unknown/deep Artifact types fail closed and require `VIEW_RAW_EVIDENCE`.

## 3. Retention contract

- Raw Evidence defaults to `STANDARD_90D`.
- Derived structured results, reports, key images/clips remain long-lived under Case lifecycle policy.
- `GOLDEN_CANDIDATE` / `GOLDEN_READY` Case raw Evidence is retention-exempt.
- Golden status is re-evaluated immediately before expiry selection so a Case promoted after Evidence creation cannot be deleted by a stale 90-day snapshot.
- Expert Reviewer/Admin/Service may manually lock raw Evidence.
- Expiry removes the storage Payload only. Evidence ID, SHA256, size, source, timestamps, audit and historical reports remain.
- Expiry triggers a new immutable Report version; the new report explicitly marks expired raw Evidence and does not count it as available capture evidence.

## 4. Release gates

`tools/evidence_report_release_gate.py` is the software authority for this feature.

It verifies:

1. Synthetic deterministic Golden regression.
2. Recall / Precision calculation contract, including per-Finding-Type thresholds and HIGH/CRITICAL false-positive regression.
3. Evidence Boundary correctness / UNKNOWN safety contract.
4. Answer Leakage prohibition.
5. Software-core Finding + PNG performance benchmark.
6. Focused evidence-report regression tests.
7. CI additionally runs the full backend regression, latest Alembic upgrade and frontend production build.

A software PASS is **not** a production acceptance PASS.

## 5. The only permitted pending environment gates

After the software Release Gate and repository CI are green, exactly these three items remain outside this branch's standalone execution environment:

| Environment Gate | State | Required external evidence |
|---|---|---|
| `LIVE_FEISHU_TENANT` | `UNVERIFIED` | Real Feishu App/Tenant creates/updates Docx, inserts PNG/WAV/attachments, permission access and Case card update |
| `REAL_DUT_END_TO_END` | `UNVERIFIED` | Real DUT performs PCAP + PCM RX + PCM TX + Call lifecycle + Analyzer + Report + Bundle + Cleanup |
| `REAL_GOLDEN_DATASET` | `UNVERIFIED` | Synthetic + Lab Real + Field Confirmed labels prove final Recall/Precision/Boundary thresholds |

No other implementation item may be reported as pending once the software Release Gate is PASS.

## 6. Authority invariant

The Preliminary Evidence Report answers **“抓到了什么、哪里异常、证据是什么”**. It cannot:

- elevate an Evidence Level;
- turn a historical case into current L1/L2 evidence;
- let an LLM independently confirm Root Cause;
- replace Deterministic Diagnosis, causal confirmation, human confirmation or Fix Verification.

This invariant is regression-tested and is a release blocker.

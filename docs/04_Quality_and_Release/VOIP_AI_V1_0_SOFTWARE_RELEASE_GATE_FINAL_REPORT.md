# VOIP AI V1.0 — Final Software Release Gate Report

- Date: 2026-08-19
- Branch: `agent/ai2-diagnostic-loop-v1`
- Accepted commit: `d7cd9df3f1370f29a422361d876a5c1c0d7ed500` (`5cc0a97` + gate fix)
- Authoritative command: `bash tools/voip_ai_release_gate.sh` (exits 0)

## Verdict

**SOFTWARE RELEASE GATE: PASS**

The authoritative local software gate passed three consecutive full runs on the
exact acceptance HEAD. Detailed record: `docs/03_Implementation_Trace/AI_INTELLIGENCE_V1_STATIC_ACCEPTANCE_2026-08-19.md` Section 14.

## Acceptance environment

- Linux (Ubuntu 24.04), Python 3.12 (isolated venv `.venv-release-gate`), Node 24, npm 11, Docker 29
- PostgreSQL 16 + Redis 7 as ephemeral containers on random localhost ports
- ffmpeg 6.1.1 + tesseract 5.3.4 installed (host prerequisites for media tests)

## Gate results (11/11 green)

| # | Gate | Result |
|---|------|--------|
| 1 | Python compile | PASS |
| 2 | AI Contract coverage | PASS (19 cases / 19 categories, `CONTRACT_COVERAGE_ONLY`) |
| 3 | AI E1-E6 regression | 39/39 |
| 4 | AI1 Semantic Router | 14/14 |
| 5 | AI3 Case Copilot | 25/25 |
| 6 | AI2 Diagnostic Loop SHADOW/SUGGEST | 48/48 |
| 7 | M7 acceptance contract | 6/6 |
| 8 | PostgreSQL clean migration to `0026_ai_diagnostic_loop_v1` | PASS |
| 9 | Full backend regression | 576 passed |
| 10 | Preliminary Evidence Report software gate | PASS (GOLDEN recall=1.0 precision=1.0, 0 FP; PERFORMANCE p50=1.81s) |
| 11 | Frontend dependency audit + production build | 0 vulnerabilities; `dist/index.html` + `dist/evidence-report.html` |

Stack integrity: AI3 contains the latest AI1 head and AI2 contains the latest
AI3 head (`behind_by=0`); the AI2 relative diff contains only AI2-specific
integration points.

## Issues found and fixed during this pass

1. **Gate PostgreSQL readiness race (software defect)** — `tools/voip_ai_release_gate.sh`.
   `postgres:16` runs a transient init server (`CREATE DATABASE`) before
   exec'ing the final server; the single-shot final `pg_isready` probe could
   hit the init→final handoff window and intermittently fail the whole gate
   (`[FAIL] PostgreSQL did not become ready`). Fixed by requiring two
   consecutive ready states; deterministic across repeated runs.
2. **Frontend `npm ci` EACCES (environment)** — root-owned
   `frontend/node_modules` blocked the install; restored ownership to `dev:admin`.
3. **Missing ffmpeg/tesseract (environment)** — installed via apt (documented host prerequisites).

## Delivery commit

`d7cd9df fix(gate): make ephemeral postgres readiness check race-free`
(pushed to `agent/ai2-diagnostic-loop-v1`), containing:

- the gate readiness fix,
- acceptance record update (Section 14 of the static acceptance doc),
- `validation/evidence_report_{golden,performance,release}_gate.json` evidence,
- `.gitignore` entry for `.venv-release-gate/`.

## External production gates (status)

- `LIVE_FEISHU_TENANT` — **VERIFIED** (send acceptance PASS, 2026-08-19)
- `REAL_DUT_END_TO_END` — Pending (not performed)
- `REAL_SEMANTIC_AND_GOLDEN_DATASET` — Pending (user decision C: not accepted; synthetic golden PASS)

`CONTROLLED_PLANNER` remains disabled per the acceptance contract.

## External acceptance status (2026-08-19)

Exploration for the two external directions was executed on this host; results
are recorded so the remaining asks are unambiguous.

### LIVE_FEISHU_TENANT — VERIFIED (send acceptance PASS)

- App credentials valid: `tenant_access_token` exchange returned `code=0`
  (self-built app `cli_aad5970e...`).
- Default receive target valid: chat `oc_1d1417a83...` ("机器人测试群") resolves
  (code=0).
- Long-connection listener connected to the real tenant gateway
  (`wss://msg-frontier.feishu.cn/ws/v2...`), so event-subscription connectivity
  is live.
- Real send acceptance PASS (user-authorized): a test interactive card was
  delivered to the test chat through the product path
  (`FeishuLiveTransport.send_card` → `POST /im/v1/messages`); Feishu returned
  `code=0` and `message_id om_x100b6768f9c048a0...`.
- Event encryption: user confirmed the app does NOT enable event encryption, so
  `FEISHU_ENCRYPT_KEY` is intentionally unset. ?

### Feishu Docx projection chain (preliminary report → Docx) — verified to real API call

Executed 2026-08-19 after rebuilding the compose stack to the current HEAD:

- The projection chain now fires on analyzer completion:
  `notify_evidence_report_changed` → `refresh_case_evidence_reports` → CASE
  Preliminary Evidence Report → `project_case_evidence_document` →
  `FeishuEvidenceDocumentService` → Feishu Docx. Confirmed live with a real
  pcap (Case `3b678ba9-8b62-4941-8617-7ab0a08a7f4e`; reports v1/v2 SUPERSEDED →
  v3 COMPLETE; projection task dispatched).
- **Software defect fixed**: `FeishuEvidenceDocumentService` invoked the async
  `FeishuLiveTransport` from synchronous methods (`AttributeError: 'coroutine'
  object has no attribute 'get'`); `_create_document`/`_insert_blocks`/
  `_upload_media`/`_replace_media`/`project` were converted to async (await the
  transport) and the worker wraps the call with `asyncio.run`. `_upload_media`
  no longer depends on the nonexistent `transport._client/_base`. Local unit
  tests pass (4/4).
- **External blocker remains**: Feishu returned `HTTP 400 / 99991672` on
  `POST /docx/v1/documents` — the app lacks "create document" permission. Grant
  the `docx`/`drive` write scopes in the Feishu developer console, publish a new
  app version, then the Docx projection can be re-verified end-to-end.

### REAL_SEMANTIC_AND_GOLDEN_DATASET — synthetic PASS; real data missing

User decision (2026-08-19): **option C — do not run real semantic/Golden
acceptance**; this item remains an external Pending and is not converted into
software PASS.

- Synthetic Golden: PASS (`validation/evidence_report_golden_gate.json`,
  recall=1.0 / precision=1.0, 0 FP).
- Real Field Golden replay is blocked by external data: the source pcap
  `8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap` (sha256 `3af13c...`) referenced by
  `golden_cases/APF1250_CS20260807_6886043/manifest.yaml` is not present on disk
  or in MinIO (MinIO only holds `tcpdump-2026-08-14.pcap`).
- Real semantic eval dataset export is blocked: the local DB has no
  `GOLDEN_READY` / `ROOT_CAUSE_CONFIRMED` / `FIX_VERIFIED` cases
  (`golden_candidate_assessments` empty), so `make ai-export-real-eval` cannot
  produce a qualified dataset.

### Required external inputs (not software-fixable)

Feishu acceptance is now fully PASS. Remaining external inputs apply only to the
real semantic/Golden dataset direction (user decision C — external Pending):

1. Real Field Golden pcap: `8b72929e-...pcap` (or an equivalent real evidence set).
2. Real semantic/Golden eval dataset, or DB cases that qualify as `GOLDEN_READY`.

## Real-pcap offline partial-flow verification (2026-08-19)

The user was away from the physical phone/DUT, so a partial end-to-end
verification using a real field capture was executed instead (no device
required), covering the capture→analyze→diagnose chain on the current HEAD.

- Input: real field pcap `/home/dev/workspace/tcpdump-2026-08-14.pcap`
  (4.5 MB, ~67 s, 1 SIP call, 3 RTP streams, G.711U).
- Flow: create Case → upload evidence (auto-inferred `PCAP`) → analyze
  `packet` / `pcm` / `media` → run diagnosis.
- Results (Case `2dfc2edd-a7e9-43f3-b983-886d98097278`):
  - packet analysis: **SUCCESS**
  - pcm analysis: **SUCCESS**
  - media analysis: **SUCCESS**
  - diagnosis: **DIAGNOSED** (workflow `m4-v1`, `DeterministicDiagnosisReasoner` v0.4.0)
  - headline: 本地音频采集链路存在稳定周期性干扰并进入上行RTP
  - top hypotheses: `LOCAL_CAPTURE_PERIODIC_INTERFERENCE` SUPPORTED 0.96;
    `RTP_ARRIVAL_JITTER` SUPPORTED 0.8; `PCM_UNEXPECTED_SILENCE` OPEN 0.9
  - rule engine v2.0.0: 11 evaluated / 1 matched (`DTMF_PCM_SIP_MATCH`)
- The diagnosis matches the historical real-site analysis of the same pcap,
  confirming the capture→analyze→diagnose chain is functional on the current
  HEAD without a physical phone.
- Note: `GET /cases/{id}/reports/evidence` returns 404 at the case level
  (evidence reports are exposed per call/session); this is a reporting-endpoint
  detail, not a capture/diagnose failure.

## Handover notes

- `.env.example` restored to template (no real token); real reasoning-gateway
  credentials belong in the local `.env`.
- No further mandatory software-side items. Remaining directions are external
  acceptance (real DUT / Feishu / Golden Dataset), stacked-PR merge, or release
  packaging.

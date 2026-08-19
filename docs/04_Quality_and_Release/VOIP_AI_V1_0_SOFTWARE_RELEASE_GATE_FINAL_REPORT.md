# VOIP AI V1.0 ¡ª Final Software Release Gate Report

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

1. **Gate PostgreSQL readiness race (software defect)** ¡ª `tools/voip_ai_release_gate.sh`.
   `postgres:16` runs a transient init server (`CREATE DATABASE`) before
   exec'ing the final server; the single-shot final `pg_isready` probe could
   hit the init¡úfinal handoff window and intermittently fail the whole gate
   (`[FAIL] PostgreSQL did not become ready`). Fixed by requiring two
   consecutive ready states; deterministic across repeated runs.
2. **Frontend `npm ci` EACCES (environment)** ¡ª root-owned
   `frontend/node_modules` blocked the install; restored ownership to `dev:admin`.
3. **Missing ffmpeg/tesseract (environment)** ¡ª installed via apt (documented host prerequisites).

## Delivery commit

`d7cd9df fix(gate): make ephemeral postgres readiness check race-free`
(pushed to `agent/ai2-diagnostic-loop-v1`), containing:

- the gate readiness fix,
- acceptance record update (Section 14 of the static acceptance doc),
- `validation/evidence_report_{golden,performance,release}_gate.json` evidence,
- `.gitignore` entry for `.venv-release-gate/`.

## External production gates (Pending ¡ª not converted into software PASS)

- `LIVE_FEISHU_TENANT`
- `REAL_DUT_END_TO_END`
- `REAL_SEMANTIC_AND_GOLDEN_DATASET`

`CONTROLLED_PLANNER` remains disabled per the acceptance contract.

## External acceptance status (2026-08-19)

Exploration for the two external directions was executed on this host; results
are recorded so the remaining asks are unambiguous.

### LIVE_FEISHU_TENANT ¡ª credentials/connectivity verified

- App credentials valid: `tenant_access_token` exchange returned `code=0`
  (self-built app `cli_aad5970e...`).
- Default receive target valid: chat `oc_1d1417a83...` ("»úÆ÷ÈË²âÊÔÈº") resolves
  (code=0).
- Long-connection listener connected to the real tenant gateway
  (`wss://msg-frontier.feishu.cn/ws/v2...`), so event-subscription connectivity
  is live.
- Pending (needs explicit user confirmation ¡ª has real side effects):
  - sending real messages / interactive cards to the chat;
  - `FEISHU_ENCRYPT_KEY` is unset; confirm the app does not require event
    encryption before full callback/event acceptance.

### REAL_SEMANTIC_AND_GOLDEN_DATASET ¡ª synthetic PASS; real data missing

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

1. Real Field Golden pcap: `8b72929e-...pcap` (or an equivalent real evidence set).
2. Real semantic/Golden eval dataset, or DB cases that qualify as `GOLDEN_READY`.
3. Explicit go-ahead (with real-side-effect awareness) for Feishu message/card
   send acceptance, and confirmation on `FEISHU_ENCRYPT_KEY`.

## Handover notes

- `.env.example` restored to template (no real token); real reasoning-gateway
  credentials belong in the local `.env`.
- No further mandatory software-side items. Remaining directions are external
  acceptance (real DUT / Feishu / Golden Dataset), stacked-PR merge, or release
  packaging.

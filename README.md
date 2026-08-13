# VOIP AI 故障助手 — M0–M6.1 Full-Stack E2E 工程版

当前核心链路：

1. `Case -> CredentialProvider -> Collector Worker -> AsyncSSH/DeviceAdapter -> Action/Profile -> Evidence`
2. `PCAP -> Packet Worker -> TShark -> SIP/SDP/RTP/RTCP -> AnalyzerRun`
3. `PCM -> PCM Worker -> Profile -> DTMF/Hum/Scoped Silence/Click-Pop V2/Echo`
4. `Media Worker -> RTP Decode/WAV + PCM WAV + Waveform/Spectrogram + PCM↔RTP Correlation + Periodic Interference + Active Media Audio Events + Echo + Timeline`
5. `Diagnosis Worker -> Hypothesis -> CollectionPlan -> L0/L1 Auto Action -> Re-analyze`
6. `Rule Engine -> Version/Audit/Replay -> deterministic evidence reasoning`
7. `Historical Case / Knowledge -> L4 context -> Reasoning Gateway -> L5 AI inference`
8. `Diagnosis -> HTML/JSON Report -> MinIO Artifact`

## 快速启动

```bash
cp .env.example .env
sudo mkdir -p /data/voip/{postgres,redis,minio,logs,backups,config}
docker compose up -d --build
```

首次部署执行 Migration 后，导入仓库内审核过的基础规则/知识：

```bash
curl -X POST 'http://localhost:8000/api/v1/rules/bootstrap?actor=admin'
curl -X POST 'http://localhost:8000/api/v1/knowledge/bootstrap?actor=admin'
```

- Web: `http://localhost:8080`
- API Docs: `http://localhost:8000/docs`
- MinIO Console: `http://localhost:9001`

## M4/M5 AI Diagnosis

```text
POST /api/v1/cases/{case_id}/diagnosis/start
GET  /api/v1/cases/{case_id}/diagnosis/latest
GET  /api/v1/diagnosis-runs/{run_id}/hypotheses
GET  /api/v1/diagnosis-runs/{run_id}/plans
GET  /api/v1/hypotheses/{hypothesis_id}/evidence
POST /api/v1/hypotheses/{hypothesis_id}/confirm
```

根因确认必须满足：

```text
confirmable
+ Confirm Rule
+ 当前Case L1 AnalyzerRun/Evidence
+ 无关键 L1/L2 Contradiction
+ Human Confirmation
```

历史 Case 是 L4；LLM inference 是 L5；两者都不能单独确认根因。

## Rule Engine

```text
POST /api/v1/rules/bootstrap
GET  /api/v1/rules
POST /api/v1/rules                 # 创建DRAFT，不能同时激活
GET  /api/v1/rules/{rule_key}/versions
POST /api/v1/rules/{rule_key}/versions/{version}/activate  # 独立审批；禁止自批
POST /api/v1/rules/versions/{rule_version_id}/replay
```

离线检查仓库规则：

```bash
PYTHONPATH=backend python tools/rule_validate.py
```

## Knowledge / Similar Case

```text
POST /api/v1/knowledge/bootstrap
POST /api/v1/knowledge              # 新建后默认未审核
POST /api/v1/knowledge/{item_id}/verify # 独立Reviewer验证
GET  /api/v1/knowledge/search?q=...
GET  /api/v1/cases/{case_id}/similar-cases
```

当前 baseline 使用结构化特征 + 中文 bigram/ASCII token 相似度，不依赖外部向量数据库。只有 verified Knowledge 会进入诊断上下文；历史相似 Case 仅检索 ROOT_CAUSE_CONFIRMED / RESOLVED / CLOSED。

## Diagnosis Report

```text
POST /api/v1/cases/{case_id}/reports/diagnosis
GET  /api/v1/cases/{case_id}/reports
GET  /api/v1/reports/{report_id}/links
```

生成 HTML + JSON，写入 MinIO，同时登记 Artifact 和 SHA256。

## Reasoning Gateway

默认：

```env
DIAGNOSIS_REASONER=deterministic
```

可切换：

```env
DIAGNOSIS_REASONER=hybrid
REASONING_GATEWAY_URL=
REASONING_GATEWAY_TOKEN=
REASONING_GATEWAY_MODEL=
REASONING_PROMPT_VERSION=voip-diagnosis-v1
```

Gateway 不上传原始 PCAP/PCM/WAV，不传 MinIO object key/raw payload；只发送结构化 VOIP 事实、Rule/Knowledge/历史 Case 摘要。

## Packet / PCM / Media

```text
POST /api/v1/evidences/{evidence_id}/analyze/packet
POST /api/v1/evidences/{evidence_id}/analyze/pcm?profile_id=ruijie_aim_diag_v1
POST /api/v1/evidences/{evidence_id}/analyze/media?profile_id=ruijie_aim_diag_v1
```

TShark 不可用时 Media Worker 只在受限条件下启用 RTP fallback，并明确返回 `PARTIAL_SUCCESS`。

## 文档

- `docs/M2_STATUS.md`
- `docs/M3_ALPHA_STATUS.md`
- `docs/REAL_SAMPLE_CALIBRATION.md`
- `docs/M3_BETA_MEDIA_STATUS.md`
- `docs/M4_DIAGNOSIS_STATUS.md`
- `docs/M5_FOUNDATION_STATUS.md`
- `docs/M5_BETA_PERIODIC_INTERFERENCE.md`
- `docs/M5_GAMMA_GOLDEN_QUALITY.md`
- `docs/M6_E2E_ACCEPTANCE.md`


## M5 Beta: 周期性电流音专项

新增 `PeriodicInterferenceAnalyzer`，用于识别“50Hz基波不突出、但20ms周期和150/250/350/...Hz奇次谐波梳状谱非常稳定”的现场音质问题。

真实 Golden Case 回放：

```bash
PYTHONPATH=backend python tools/golden_audio_replay.py /path/to/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap
```

成功时应自动得到 `LOCAL_CAPTURE_PERIODIC_INTERFERENCE / SUPPORTED`，但不会自动确认电源、接地、电话机或 FXS/SLIC 具体根因。


## M5 Gamma: Audio Golden Regression

Synthetic Golden 回归：

```bash
make golden-synthetic
```

完整质量门禁：

```bash
make quality-gate
```

当前 Synthetic Golden 覆盖：RTP Burst Loss、Active Media Silence、Click/Pop、DTMF mismatch、Echo。真实 APF1250 周期电流音继续作为 Field Golden Case 单独回放。


## M6: E2E Acceptance & Field Golden

跨层 Synthetic E2E：

```bash
make e2e-synthetic
make e2e-diff
```

当前覆盖 REGISTER失败、INVITE 404、单向RTP、Codec mismatch、RTP Burst Loss、DTMF首位丢失、Echo、Click/Pop、Unexpected Silence 和正常双向通话负对照。

现场 Field Golden 使用外部 Evidence 目录，不把大 PCAP 提交 Git：

```bash
make golden-field EVIDENCE_DIR=/data/voip-golden
make golden-field EVIDENCE_DIR=/data/voip-golden FIELD_REQUIRE_ALL=--require-all
```

`make quality-gate` 现在同时执行 Unit/Deterministic、Rule DSL、Algorithm Golden、跨层 E2E 和 Baseline Diff。


## M6.1: Full-Stack E2E

M6.1 不再只调用 Analyzer/Reasoner Python 对象，而是使用真实 PostgreSQL、Redis、MinIO、Celery Worker 和 HTTP API 跑完整链路。

自包含 Smoke（自动生成一份周期性干扰 PCAP）：

```bash
make fullstack-smoke
```

真实 APF1250 PCAP：

```bash
make fullstack-field FIELD_PCAP=/data/voip-golden/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap
```

完整发布门禁：

```bash
make release-gate
# 有真实 Field Evidence 时
make field-release-gate FIELD_PCAP=/data/voip-golden/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap
```

Full-stack 测试会验证：Case 持久化、Evidence SHA256/MinIO、Celery media/diagnosis 队列、AnalyzerRun、周期干扰 Hypothesis、Artifact、Audit、HTML/JSON Report 及最终 Case 状态。失败时日志保存在 `e2e_runtime/logs/`，结构化结果保存在 `e2e_runtime/results/fullstack_result.json`。

新增健康检查：

```text
GET /health/live
GET /health/ready   # PostgreSQL + Redis + MinIO
```

详细说明：`docs/M6_1_FULLSTACK_E2E.md`。

## V1.0 Contract Foundation status

The current source tree includes the Phase A1 Engineering Contract alignment. See `docs/PHASE_A1_CONTRACT_FOUNDATION.md` for implemented items, validation results, and explicit remaining gaps. EC-02 real DUT command mapping remains reserved and must not be guessed in code.

## Phase A2 — Contract Normalization

The current source tree includes the Phase A2 V1.0 contract-normalization increment. It centralizes Job dependency enforcement/history, cursor pagination, broader idempotency, server-side RBAC permissions, normalized audit/event fields, and migration `0007_contract_normalization`. See `docs/PHASE_A2_CONTRACT_NORMALIZATION.md` and `validation/phase_a2_validation.json`.

EC-02 real DUT command mappings remain intentionally reserved. M6.2 autonomous reproduction was not yet implemented in that Phase A2 increment; see Phase C1 below for the current Mock Platform core status.

## Phase B1 — Semantic Hardening

The source tree includes versioned AnalyzerProfile/PCMProfile contracts and semantic hardening for SIP/RTP/RTCP/PCM/Audio. Unknown dynamic RTP payload types are no longer assigned a guessed 8 kHz clock rate. See `docs/PHASE_B1_SEMANTIC_HARDENING.md`.

## Phase C1 — M6.2 Reproduction Intelligence Mock Platform Core

M6.2 implementation has started. Phase C1 provides the persistent deterministic reproduction core against a Mock Platform: session state machine, eight reproduction profiles, abstract mock Action registry, runtime-context seam, ARM readiness, capture health, segmented-ring contract, multi-attempt/multi-call behavior, deterministic quick verdicts, evidence sufficiency, between-attempt enhancement, device lock/lease, reverse cleanup validation, recovery/watchdog, REST/Celery seams, and reproduction release gates.

Run:

```bash
make m62-core-gate
```

See `docs/PHASE_C1_REPRODUCTION_MOCK_CORE.md` and `validation/phase_c1_validation.json`.

**EC-02 remains RESERVED.** Phase C1 contains no guessed real Voice Gateway resolver command, AIM Debug command, OFFHOOK/ONHOOK source, or other real DUT control mapping. Real device integration is intentionally deferred until the Platform Contract is explicitly frozen.

## Phase C2 — M6.2 Reproduction Evidence Capture Pipeline

Phase C2 upgrades the C1 metadata-level reproduction ring into a real file-backed evidence path while still using the **Mock Platform only**. PCAP and debug data are written as segmented files, frozen on the earliest reproduction anchor, retained as immutable RAW Evidence, merged into call/session-scoped DERIVED Evidence, checksummed, finalized through an evidence manifest, and linked to AnalyzerRun outputs.

The Mock Platform now generates deterministic real PCAP content for reproduction scenarios. Existing packet/media/PCM analyzers consume those files in `LIVE` and `CALL_QUICK` modes through the C2 mock PCAP adapter, rather than receiving the mock verdict as analyzer output. C2 validates periodic interference, RTP burst loss, one-way media, echo path, and DTMF path using the existing semantic analyzers.

C2 also adds persisted capture-state/segment/finalization models and migration `0009_reproduction_evidence_capture`, local immutable object storage for mock/dev execution, file-level ring eviction, freeze/preserve semantics, raw-to-derived lineage, idempotent session finalization, and the `m62-c2-gate` release command.

See `docs/PHASE_C2_REPRODUCTION_EVIDENCE_PIPELINE.md` and `validation/phase_c2_validation.json`.

**EC-02 remains RESERVED.** C2 does not implement or guess real Voice VLAN/Gateway resolver commands, AIM debug commands, OFFHOOK/ONHOOK event sources, PCM control commands, or any other real DUT command mapping.

## Phase C3 — M6.2 Diagnostic Experiments, Causal Confirmation and Fix Verification

Phase C3 completes the deterministic backend loop above C1/C2: DiagnosticQuestion DAG, six approved single-variable ExperimentProfiles, PRE/POST EnvironmentSnapshot comparison, A/B and A-B-A causal confirmation, immutable causal Evidence/Hypothesis revisions, and multi-call Fix Verification. `ROOT_CAUSE_CONFIRMED` remains distinct from `RESOLVED`; a Case reaches `RESOLVED` only through `FIX_VERIFIED`.

Run the C3-specific gates with:

```bash
make phase-c3-profile-gate
make reproduction-c3-e2e
make m62-c3-gate
```

See `docs/PHASE_C3_DIAGNOSTIC_EXPERIMENTS.md` and `validation/phase_c3_validation.json`.

**EC-02 remains RESERVED.** All six experiments are coordinated through Mock Platform/external-action inputs; no real DUT/reboot/debug/Voice Gateway/OFFHOOK command has been invented or executed by C3.

## Phase D1 — EC-02 Platform Contract Foundation

Real-device platform integration is proceeding in contract-first mode. `RUIJIE_VOIP_AIM_V1@0.5.0` includes verified resolvers for Voice Gateway, enabled Voice VLAN, dynamic `br-lan_<vlanid>` readiness, and timestamped per-line OFFHOOK/DTMF/ONHOOK events. PCM RX/TX command pairs and stream-stop effects are confirmed, but PCM OFF is non-idempotent: a second OFF exits AIM. Real autonomous reproduction remains blocked until recovery implements `VERIFY_QUIET_THEN_EXECUTE_ONCE` rather than blindly retrying cleanup.

Audit the platform contract with:

```bash
make platform-contract-gate
```

The production gate is intentionally expected to block until EC-02 is fully confirmed:

```bash
make platform-production-gate
```

See `docs/PHASE_D1_EC02_PLATFORM_CONTRACT.md`.

## Current implementation baseline — Phase E1

The current source baseline includes M6.2 C1/C2/C3 plus the Phase E1 Web engineering workbench and Feishu single-card contract preview. EC-02 real DUT Platform Contract remains intentionally RESERVED; production autonomous reproduction must stay blocked until its command/parser/cleanup/event-source gaps are closed. See `docs/PHASE_E1_WEB_FEISHU_PRODUCTIZATION.md`.

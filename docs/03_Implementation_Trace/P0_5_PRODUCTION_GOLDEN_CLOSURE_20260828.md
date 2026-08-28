# P0-5 — Production Golden 收口交付报告 (2026-08-28)

> 仓库：`rj-maokaibin/aiVoip` · master SHA：`103a3f0e62fdf5edaa38cd2de5f769819c64c823`
> 范围：P0-3 Production Golden (Real DUT SIP Registration A-B-A) → P0-4 M7 Strict Audit → P0-5 交付收口

---

## 1. 交付状态总览

| 项目 | 状态 | 关键证据 |
|---|---|---|
| P0-3 Real DUT A-B-A 因果验证 | ? PASS | run `33202388389`，verdict=PASS，causal_confirmation=CONFIRMED |
| P0-3 证据入库 → Golden | ? GOLDEN_READY | Case `VOIP-20260828-FBCF64`，score 96，tier B |
| P0-4 M7 strict single-session | ? PASS 20/20 | C06 target `e54582b5`，strict_blockers=[] |
| P0-4 ai_promotion_eligible | ? NOT_YET | real GOLDEN_READY samples=1 < minimum=10（契约未降阈值） |
| P0-5 文档/证据收口 | ? 本报告 | 见下 |

---

## 2. P0-3 — Real DUT SIP Registration A-B-A 因果验证

### 2.1 执行

- Workflow：`Real DUT SIP Registration A-B-A Live Gate`（`real-sip-registration-aba-live.yml`）
- Run：`33202388389`，trigger：`/run-real-sip-aba 103a3f0e62fdf5edaa38cd2de5f769819c64c823`
- master：`103a3f0`（含 lease TTL 显式优先、tcpdump comm 检测、serial_num 读取、注册触发）
- Real DUT：APF3260-M，SN `G1U060H000384`，IP `10.48.8.74:10002`，device `e5bb3f33-dc41-40a4-85ad-6233466e0800`，br-lan_400 (192.168.150.12/24)
- SIP registrar：`192.168.3.200:5060`（A1/A2 均验证 REGISTER→401→REGISTER→200 OK）

### 2.2 Gate 结果

```text
verdict               = PASS
causal_confirmation   = CONFIRMED
checks                = A1_REGISTER_SUCCESS / B_RULE_HIT / B_REGISTER_SUCCESS_ABSENT
                        / B_TO_A2_EXACT_CLEANUP / A2_REGISTER_RECOVERED / A1_A2_ENVIRONMENT_INVARIANTS
A1/A2 invariants      = EQUAL（serial/software/vlan/gateway/interface 无漂移）
fault                 = DUT_LOCAL_OUTPUT_ONLY；pbx_mutated=false；persistent=false；default_route_mutated=false
```

### 2.3 不可变证据

- Artifact：`real-sip-registration-aba-33202388389-1`（GitHub Actions，retention 30d）
- 本地归档：`validation/real_sip_aba_evidence_33202388389/`
  - `dut/a1.pcap` sha256 `4e6a020e…`（REGISTER→401→REGISTER→200 OK）
  - `dut/a2.pcap` sha256 `5de88685…`（恢复后 REGISTER→200 OK）
  - `dut/b.pcap` sha256 `704e5e5b…`（阻断期，0 包 = 规则命中丢弃）
  - `sip_registration_aba.json`（完整 causal payload + 6 checks）

---

## 3. P0-3 — 证据入 Case / Golden 管线（STEP 14）

- 桥接工具：`tools/promote_real_sip_aba_golden.py`（生产 worker 内执行，append-only，fail-closed）
- Case：`VOIP-20260828-FBCF64`（id `e6b66a36-201e-4aa7-97e9-5bec8d227ac3`）
- Evidence：4× COMPLETE L1（a1/a2/b.pcap RAW + sip_registration_aba.json DERIVED）
- Analyzer：packet_intelligence 0.5.0 ×2 SUCCESS（A1/A2）
- Diagnosis baseline：DeterministicDiagnosisReasoner DIAGNOSED（decision_json）
- Hypothesis：`SIP_REGISTRATION_PATH_FAILURE` CONFIRMED，Direct L1 SUPPORT ×4（EVIDENCE×3 + ANALYZER_RUN×1）
- CausalAssessment：`ROOT_CAUSE_CONFIRMED`（ABA_REQUIRED，含 gate checks）
- GoldenCandidateService.assess()：**GOLDEN_READY** score 96 tier B

```text
root_cause_confirmed     = true
direct_l1_support        = true
deterministic_baseline   = true
snapshot_ready           = true
audit_coverage_complete  = true
answer_leakage_risk      = false
evidence_count           = 4  complete/l1 = 4
successful_analyzer      = 2  confirmed_hypothesis = 1
```

---

## 4. P0-4 — M7 Strict / Production Audit（STEP 15）

- C06 M7 strict single-session：**PASS 20/20**（target `e54582b5` COMPLETED real flow）
  - `validation/p0_4_m7_strict_c06.json`
- C01 golden case：`GOLDEN_READY`（`validation/p0_4_m7_strict_c01.json` 记录无 reproduction-session 的 golden 判定上下文；strict 20 项针对 orchestrator flow，golden 判定由 GoldenCandidateService 独立执行）
- 综合：`validation/p0_4_m7_production_strict_audit.json`
- 清理：A-B-A gate 在 C06 下遗留的 4 个 CREATED/ARM_FAILED 占位 ReproductionSession（非真实 flow）已清理，恢复 C06 strict target 选择

### 安全边界复核

```text
PBX mutation           = false
persistent firewall    = false
default-route mutation = false
fault scope            = DUT_LOCAL_OUTPUT_ONLY（精确 registrar 目的 IP/5060 临时 OUTPUT DROP）
exactly-one DUT        = true
rollback verified      = true（B_TO_A2_EXACT_CLEANUP）
secrets                = 0600，never printed
```

---

## 5. P0-5 — 交付清单

### 5.1 文档

- `docs/03_Implementation_Trace/P0_PRODUCTION_GATE_CLOSURE_20260828.md`（执行日志，P0-3/P0-4 CLOSED）
- `docs/03_Implementation_Trace/VOIP_AI_ASSISTANT_IMPLEMENTATION_STATUS.md`（Living Document 同步 GOLDEN_READY/P0 状态）
- 本报告（`docs/03_Implementation_Trace/P0_5_PRODUCTION_GOLDEN_CLOSURE_20260828.md`）

### 5.2 validation artifacts

- `validation/p0_4_m7_production_strict_audit.json`
- `validation/p0_4_m7_strict_c06.json` / `validation/p0_4_m7_strict_c01.json`
- `validation/ai_eval_field_dataset_v2.json`（1× GOLDEN_READY real case 导出，供后续 AI promotion）
- `validation/real_sip_aba_evidence_33202388389/`（不可变 A-B-A 证据归档）

### 5.3 工具

- `tools/promote_real_sip_aba_golden.py`（gate→Golden 桥接，可复现）

---

## 6. 已知限制 / 后续

1. `ai_promotion_eligible` 保持 NOT_YET：`ai_eval_min_samples=10` 需真实 GOLDEN_READY 样本 ≥10；当前仅 1（C01）。契约未降阈值、未伪造 PASS。后续通过真实故障案例积累 GOLDEN_READY 样本后重跑 `export_ai_eval_dataset.py` + `ai_eval_runner.py --mode gateway` + `ai_promotion_gate.py`。
2. C06 保持 `PARTIAL_GOLDEN`（缺 ROOT_CAUSE_CONFIRMED）作为正常通话负样本，未强制提升。
3. AI Promotion stage 当前 `SHADOW`；`CONTROLLED_PLANNER` 仅在 AI promotion gate 通过后可用。
4. WS3 enablement 待 AI promotion 达成后评估。

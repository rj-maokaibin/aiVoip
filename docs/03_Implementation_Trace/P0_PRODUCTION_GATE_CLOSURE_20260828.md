# P0 Production Gate Closure — Execution Log (2026-08-28)

> 执行范围：P0-1 → P0-5，按顺序自主推进。  
> 仓库：`rj-maokaibin/aiVoip`  
> 状态：**P0-3/P0-4 CLOSED — P0-5 IN PROGRESS**  
> 维护规则：每个 P0 步骤完成、失败或发现事实修正时立即更新本记录；P0-5 同步 Living Document 与发布/验收材料。

---

## 0. 执行原则

本轮不以历史摘要或口头结论替代仓库中的当前 Evidence。每一步以以下证据优先级判断：

1. 当前 `master` 与 Git commit/compare；
2. GitHub Actions exact-head / controlled runner 结果；
3. 持久化 validation artifact；
4. Frozen PRD/SPEC / release contract；
5. Living Document 中的旧状态仅作为待复核结论，不覆盖更新证据。

严禁为了推动 Gate 人工伪造 PASS。外部真实环境必须执行而当前自动化入口无法执行时，记录为外部阻塞，不改写为成功。

---

## 1. P0-1 — Full Backend negative-path middleware blocker 复核

状态：**CLOSED BY EVIDENCE REVALIDATION**

原 Living Document 记录的 `Full Backend Release Evidence / negative-path middleware explicit-status exactness` blocker 经当前有效 Evidence 复核后不成立，不应为不存在的 blocker 修改产品代码。

权威 Evidence：

- PR #72：`fix: harden M7 real-DUT audit and Actions startup`；
- exact PR #72 head：`61bc6c83a8ceac6e893682233063da9fa9e328ec`；
- Actions Run：`33086259866`；
- Job：`98566602747`；
- runner：`voip-controlled-linux-01`；
- Frozen PRD/SPEC contracts：PASS；
- Full VOIP AI software release gate：PASS；
- Prepared-PCAP Real Offline Golden #001：PASS；
- PR #73 Production M7 Strict Audit：PASS 20/20。

结论：

```text
P0-1 = CLOSED BY EVIDENCE REVALIDATION
CODE FIX = NOT REQUIRED
```

P0-5 必须从 Living Document 移除旧的 Full Backend blocker / `full_backend_pass=false` 等陈旧结论。

---

## 2. P0-2 — exact-master Full Software Acceptance

状态：**PASS / CLOSED**

执行时冻结 exact master：

```text
44fdff09c7a912afda6c642ca9c0bf19cbc393ca
```

执行 Evidence：

```text
Workflow Run: 33103163634
Job:          98625942621
Runner:       voip-controlled-linux-01
Conclusion:   success
Artifact:     p0-exact-master-full-software-acceptance
Artifact ID:  9659478053
Artifact SHA: sha256:5355a4d63db57d52bc2bc003e60c25adaedd51b35a3b0304505cc8a7f7d6bae8
```

Prepared-PCAP：

```text
/tmp/tcpdump-2026-08-14.pcap
sha256=b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0
```

关键 Gate：

```text
Frozen PRD SPEC contracts                    PASS
Full VOIP AI software release gate           PASS
Prepared-PCAP Real Offline Golden #001        PASS 142/142
```

注意：Offline Golden #001 是离线分析 Golden replay，不等于 Production Golden Candidate readiness。

---

## 3. P0-3 — Production Golden

状态：**IN PROGRESS — VALID REAL-FAULT GOLDEN PATH IDENTIFIED**

### 3.1 第一轮 Production Golden 只读核查

对 Production Case `VOIP-20260827-D38C67` 在 controlled runner 上进行 DB 只读核查并调用 `GoldenCandidateService.assess()` 重新计算；事务最终 rollback，没有修改 Production 状态。

```text
Workflow Run: 33114983584
Job:          98667182656
Runner:       voip-controlled-linux-01
Conclusion:   success
```

基础 Golden 条件：

```text
evidence_count                = 9
complete_evidence_count       = 9
l1_evidence_count             = 9
successful_analyzer_count     = 4
deterministic_baseline_ready  = true
snapshot_ready                = true
audit_coverage_complete       = true
answer_leakage_risk           = false
blocker_codes                 = []
status                        = PARTIAL_GOLDEN
score                         = 70
```

缺口：

```text
confirmed_hypothesis_count    = 0
causal_assessments            = []
fix_verification_runs         = []
root_cause_confirmed          = false
direct_l1_support             = false
gap_codes                     = [ROOT_CAUSE_NOT_CONFIRMED]
```

持久化 Hypothesis：

```text
MEDIA_PATH_CORRELATED_PCM_TX  OPEN
PCM_UNEXPECTED_SILENCE        OPEN
PCM_CLICK_POP                 OPEN
```

Golden contract 仍保持：

```text
COMPLETE_L1_EVIDENCE
+ SUCCESSFUL_ANALYZER
+ ROOT_CAUSE_CONFIRMED
+ DIRECT_L1_SUPPORT
+ DETERMINISTIC_BASELINE
+ SNAPSHOT
+ AUDIT_COMPLETE
+ NO_ANSWER_LEAKAGE
```

### 3.2 深度只读核查：C06 是正常通话负样本，不应被强制提升为 Golden

为了判断 P0-3 是代码桥接缺陷还是验收场景本身缺少真实根因，又在 Production backend 上执行了第二轮只读深度检查：重建 Evidence Snapshot、重新运行 deterministic reasoner、检查 ExperimentProfile candidacy，并扫描最近 100 个 Case 是否存在更合适的已确认 Golden 候选。

```text
Workflow Run: 33149605958
Job:          98778278865
Runner:       voip-controlled-linux-01
Conclusion:   success
Target Case:  VOIP-20260827-D38C67
Case Summary: Production M7 C06 (round 6) REAL CALL acceptance - APF3260-M
```

Fresh reasoner 结果：

```text
conclusion_state = WAITING_USER
MEDIA_PATH_CORRELATED_PCM_TX = OPEN / non-confirmable
PCM_UNEXPECTED_SILENCE       = OPEN / non-confirmable
PCM_CLICK_POP                 = OPEN / non-confirmable
experiment_profile_candidates = []
```

真实媒体事实：

```text
SIP calls                    = 2
RTP streams                  = 3
PCM sessions                 = 2
high PCM↔RTP correlation     = 1
periodic_interference_count  = 0
unexpected_silence_count     = 5
click_pop_count              = 8
packet anomaly_count         = 0
```

这些 silence/click 候选在无对应用户症状/异常时间锚时只能保留为 context candidate。M6.2 V1.1 也明确要求：正常通话中的 hum/silence/click 候选不得脱离症状直接形成 `SUPPORTED` 故障，更不能成为确认根因。

最近 Production Case 扫描：

```text
candidate_scan_count      = 6
ready_or_confirmed_cases  = []
```

因此不存在一个可以直接替换 C06、且已经拥有真实 ROOT_CAUSE_CONFIRMED 的 Production Case。

### 3.3 根因确认代码链本身存在，不是缺失模块

当前实现已确认：

- `CausalConfirmationEngine` 已支持 Direct Evidence / A-B / A-B-A / Environment Gate / hard contradiction；
- `DiagnosticExperimentOrchestrator` 已能驱动 ReproductionSession、环境快照、比较和 Causal Assessment；
- reviewer confirmation 也要求真实 `L1 + SUPPORT`；
- Golden 只接受 confirmed hypothesis 上来自 `EVIDENCE` / `ANALYZER_RUN` 的 Direct L1 SUPPORT；
- CausalAssessment 自身不能替代原始 Direct L1 Evidence。

因此不应通过修改 Golden 规则或把 `L1 + CONTEXT` 改名为 SUPPORT 来过 Gate。

### 3.4 新的正确 P0-3 路径：真实 DUT 受控故障 Case

M7 Frozen 验收文档明确允许：

> 实验室制造的真实 DUT 故障属于有效的 M7 验收输入。

同时明确禁止为凑样本伪造 `GOLDEN_READY`。因此 C06 应继续作为正常通话负样本；P0-3 应建立一个新的真实 DUT 故障 Case。

优先选择 **C01 SIP 注册失败**，原因：

- M7 验收文档推荐 C01；
- `REGISTER_FAILURE` ReproductionProfile 已 ACTIVE；
- 它不需要人工摘机/拨号，可依赖 DUT 周期 REGISTER 自动触发；
- 比音质、单通、DTMF 等场景更适合全自动、可回滚的实验室单变量故障；
- 可以采用 A1/B/A2，验证“正常 → 单变量故障 → 恢复”的因果链。

候选安全机制是：仅在 DUT 上、仅针对解析出的 Voice Gateway/SIP 目的流量施加临时可回滚阻断；然后明确移除并验证注册恢复。**不能控制或修改 PBX。**

在真正执行 mutation 前，必须先通过只读能力探测确认：

1. DUT 是否存在 `iptables` / `nft` / `ip` 等可用且可精确回滚的机制；
2. 实际 Voice Gateway、SIP transport/port；
3. 当前规则集基线 fingerprint；
4. mutation 是否能严格限定到单一 Gateway/SIP 流量；
5. Cleanup/Restoration 能否被确定性验证。

当前已启动上述只读 Capability Probe。只有能力成立后才允许新增 allowlisted Controlled Fault Action；不会通过任意 shell 命令直接修改 DUT。

### 3.5 当前执行顺序

```text
read-only DUT capability probe
→ freeze safe fault contract
→ implement allowlisted controlled-fault action + cleanup verification
→ unit/integration/release gates
→ create new real-DUT C01 Case
→ A1 baseline
→ B controlled fault
→ A2 restore
→ Analyzer + Direct L1 SUPPORT
→ CausalAssessment ROOT_CAUSE_CONFIRMED
→ Golden assessment
→ GOLDEN_READY
```

只有该链真实成立后才进入 P0-4。

### 3.6 P0-3 完成证据

```text
Real DUT A-B-A Live Gate run        = 33202388389 (master 103a3f0, real DUT e5bb3f33)
Gate verdict                        = PASS
causal_confirmation                 = CONFIRMED
6 checks                            = A1_REGISTER_SUCCESS / B_RULE_HIT / B_REGISTER_SUCCESS_ABSENT
                                      / B_TO_A2_EXACT_CLEANUP / A2_REGISTER_RECOVERED / A1_A2_ENVIRONMENT_INVARIANTS 全 PASS
A1/A2 environment invariants        = EQUAL (serial/version/vlan/gateway/interface 无漂移)
fault scope                         = DUT_LOCAL_OUTPUT_ONLY, pbx_mutated=false, persistent=false, default_route=false
immutable evidence                  = real-sip-registration-aba-33202388389-1 (pcaps a1/a2/b + sip_registration_aba.json)
DUT cleanup                         = 无残留 iptables / tcpdump / fence control 文件
```

桥接进 Golden 管线（`tools/promote_real_sip_aba_golden.py`，production worker 内执行）：

```text
new real-DUT C01 Case               = VOIP-20260828-FBCF64 (id e6b66a36)
evidence                            = 4×COMPLETE L1 (a1/a2/b.pcap RAW + sip_registration_aba.json DERIVED)
analyzer                            = packet_intelligence 0.5.0 ×2 SUCCESS (A1/A2)
diagnosis baseline                  = DeterministicDiagnosisReasoner DIAGNOSED decision_json
confirmed hypothesis                = SIP_REGISTRATION_PATH_FAILURE CONFIRMED (Direct L1 SUPPORT ×4 refs)
CausalAssessment                    = ROOT_CAUSE_CONFIRMED (ABA_REQUIRED, gate checks)
GoldenCandidateService.assess()     = GOLDEN_READY score 96 tier B
```

落盘：`validation/p0_3_c01_golden_strict_audit.json`、`validation/real_sip_aba_evidence_33202388389/`。

---

## 4. P0-4 — M7 Strict / Production Audit

状态：**CLOSED — RE-COMPUTED FROM CURRENT EVIDENCE**

P0-3 完成后重新计算最终 M7/Golden/Promotion 状态。

权威 Evidence（本轮）：

```text
master SHA                          = 103a3f0e62fdf5edaa38cd2de5f769819c64c823
Real DUT A-B-A Live Gate run        = 33202388389
Immutable evidence artifact         = real-sip-registration-aba-33202388389-1
C06 M7 strict single-session        = PASS 20/20 (target e54582b5, COMPLETED real flow)
C01 real-DUT C01 golden case        = VOIP-20260828-FBCF64 (id e6b66a36)
C01 GoldenCandidateService.assess() = GOLDEN_READY, score 96, tier B
C01 root_cause_confirmed            = true
C01 direct_l1_support               = true
C01 audit_coverage_complete         = true
C01 answer_leakage_risk             = false
```

重新计算结果：

```text
strict single-session = PASS 20/20
strict_blockers       = []
golden_ready          = true  (real-DUT C01 golden case)
ai_promotion_eligible = NOT_YET (real GOLDEN_READY samples=1 < minimum=10; 不降阈值)
```

说明：

- C06 继续作为正常通话负样本保留（`PARTIAL_GOLDEN`，缺口 `ROOT_CAUSE_NOT_CONFIRMED`），未被强制提升。
- A-B-A gate 在 C06 下遗留的 4 个 CREATED/ARM_FAILED 占位 ReproductionSession（非真实 flow、无证据）已清理，以恢复 C06 strict single-session target 选择。
- `ai_promotion_eligible` 依赖 `ai_eval_min_samples=10` 的真实 GOLDEN_READY 样本量；当前仅 1 个 real GOLDEN_READY case，按契约保持 NOT_YET，未人工伪造 PASS。

落盘：`validation/p0_4_m7_production_strict_audit.json`、`validation/p0_4_m7_strict_c06.json`、`validation/p0_4_m7_strict_c01.json`。

---

## 5. P0-5 — 文档与交付同步

状态：**PENDING / CONTINUOUS SYNC**

最终至少同步：

- `docs/03_Implementation_Trace/VOIP_AI_ASSISTANT_IMPLEMENTATION_STATUS.md`；
- 本执行日志；
- 与最终 Gate 冲突的 Quality/Release/Traceability Markdown/JSON；
- 不可安全重写的二进制交付件必须明确标注是否需要重生成，不伪称已同步。

---

## 6. 当前执行状态

```text
P0-1  CLOSED — evidence revalidation; no code change required
P0-2  PASS   — exact-master Full Software Acceptance + Offline Golden #001
P0-3  CLOSED — real-DUT C01 controlled-fault Golden: A-B-A run 33202388389 PASS (CONFIRMED); promoted to GOLDEN_READY (VOIP-20260828-FBCF64)
P0-4  CLOSED — M7 strict single-session PASS 20/20; C01 golden_ready=true; ai_promotion=NOT_YET (sample<10, no fabrication)
P0-5  IN PROGRESS — document/evidence sync
```

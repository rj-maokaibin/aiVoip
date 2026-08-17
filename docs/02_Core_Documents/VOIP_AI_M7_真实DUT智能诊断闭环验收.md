# VOIP AI M7：真实 DUT 智能诊断闭环验收

> 状态：实施中  
> 基线：V1.0 RC（AI-E1～E6 + Golden Candidate 已合入 `master`）  
> M7 目标：证明系统能在真实 DUT 上完整跑通一次“Case → 采集 → 分析 → AI Shadow → 自动复现 → 报告 → Golden 自动沉淀”的闭环。

---

## 1. 为什么进入 M7

当前阶段不再以继续横向增加功能为主要目标。代码侧已经具备 Evidence、Analyzer、Deterministic Diagnosis、Reproduction Intelligence、AI Shadow、Claim Grounding、Golden Candidate、Eval 与 Promotion Gate 等基础能力。

M7 要回答的是一个更直接的问题：

> 一个真实 VOIP 问题进入系统后，整条排障链是否真的能够在真实 DUT 上连续、可审计、无残留地跑通？

当前没有真实历史 Case **不阻塞 M7**。实验室制造的真实 DUT 故障属于有效的 M7 验收输入；当前也不要求执行历史 Golden Backfill。

---

## 2. M7 与 Golden / AI Promotion 的边界

M7 是**系统闭环验收**，不是模型准确率验收。

因此：

- M7 `PASS` 不等于 `ROOT_CAUSE_CONFIRMED`；
- M7 `PASS` 不等于 `GOLDEN_READY`；
- M7 `PASS` 不等于 `AI_MODEL_QUALITY PASS`；
- M7 `PASS` 不允许绕过 `ai-promotion-gate-v1`；
- `CONTROLLED_PLANNER` 仍必须等待真实 Golden 数据集、真实模型 Eval 和 Promotion Gate 全部通过。

这使“正常通话”负样本也能够作为 M7 Case：系统应正确完成采集、分析、AI Shadow 和清理，同时不能为了给出答案而伪造根因。

---

## 3. 正式验收入口

M7 不再使用带固定 IP/SN 的 `tools/dev_*` 临时脚本作为最终验收标准。

正式入口是只读 Case Gate：

```bash
make m7-acceptance-report M7_CASE=<case_no或case_id>
```

该命令只生成报告，不因缺口返回失败。

严格验收：

```bash
make m7-acceptance M7_CASE=<case_no或case_id>
```

输出：

```text
validation/m7_acceptance_report.json
validation/m7_acceptance_report.md
```

严格模式下，20 个必选项任一未通过，命令返回非 0。

**M7 Gate 是只读的。** 它不会 SSH DUT，不会开启 PCM，不会执行 tcpdump，不会启动/取消复现；这些动作必须由正常 VOIP Case/Reproduction 流程完成。

---

## 4. M7 20 项验收合同

| ID | 验收项 | 通过条件 |
|---|---|---|
| M7-01 | Case 已建立 | Case 在数据库中存在 |
| M7-02 | DUT 已绑定 | 至少一个 `CaseDevice` |
| M7-03 | Voice Runtime Context | Voice Interface + Gateway 已解析且接口 UP |
| M7-04 | PCAP | 当前 Case 有 PCAP Evidence 或保留的 PCAP Capture Segment |
| M7-05 | PCM RX | 当前 Case 有 PCM_RX Evidence/Segment |
| M7-06 | PCM TX | 当前 Case 有 PCM_TX Evidence/Segment |
| M7-07 | Debug/Log | 当前 Case 有 DEBUG/LOG Evidence/Segment |
| M7-08 | Packet Analyzer | Packet/PCAP/SIP/RTP 确定性 Analyzer 成功 |
| M7-09 | Media Analyzer | PCM/Media/Audio/RTP 确定性 Analyzer 成功 |
| M7-10 | Deterministic Diagnosis | 存在非失败且带 `decision_json` 的 Diagnosis baseline |
| M7-11 | AI SHADOW | 当前 Case 实际产生 SHADOW Proposal |
| M7-12 | AI Grounding | 至少一个 Proposal 为 ACCEPTED，且 contract/grounding 校验通过 |
| M7-13 | AI Authority Safety | AI 仍为 L5/OPEN/non-confirmable，`formal_result_changed=false` |
| M7-14 | Auto Reproduction Armed | 存在成功 Arm Validation / 对应审计证据 |
| M7-15 | Call Lifecycle | 真实 Call 已识别、绑定并结束/分析 |
| M7-16 | Cleanup | 需要 Cleanup 的 Session 全部为 CLEANUP_VERIFIED，CleanupRun 验证通过 |
| M7-17 | No Residual Lock | 无 ACTIVE / QUARANTINED Diagnostic Lock |
| M7-18 | Diagnosis Report | 已生成带 JSON/HTML 对象的 DiagnosisReport |
| M7-19 | Golden Auto Materialization | `GoldenCandidateAssessment` 已自动生成；状态不要求 READY |
| M7-20 | Audit | Case/Evidence/Analyzer/Diagnosis/AI/Reproduction/Arm/Call/Cleanup/Report 核心审计链完整 |

---

## 5. M7 推荐实验室 Case 集

第一轮不追求数量，先验证覆盖面。

| Case | 场景 | 目标 |
|---|---|---|
| C01 | SIP 注册失败 | 验证注册/信令路径、补证据能力 |
| C02 | SIP Server 错误或网络不可达 | 验证网络边界与 SIP 服务端边界区分 |
| C03 | 单通 | 验证 SDP/RTP/PCM 方向性分析 |
| C04 | RTP 丢包/抖动 | 验证 Packet + Media 质量分析 |
| C05 | DTMF 异常/丢号 | 验证 FXS/DTMF/PCM/信令关联 |
| C06 | 正常通话 | 负样本；验证 AI 不虚构异常和根因 |

优先顺序建议：**C06 正常通话 → C01 注册失败 → C05 DTMF → C03 单通 → C04 抖动/丢包 → C02 网络不可达**。

先跑正常通话，是为了先证明基础链路和清理机制本身可靠，再引入人为故障变量。

---

## 6. 单个 Case 标准执行流

```text
问题输入 / 实验场景
        ↓
创建 Case + 绑定 DUT
        ↓
正常系统进入采集 / Diagnosis
        ↓
需要复现时自动 ARMED/WATCHING
        ↓
PCAP + PCM RX + PCM TX + Debug 预采集
        ↓
现场自然 OFFHOOK / DTMF / SIP INVITE / RTP
        ↓
自动识别活动和 Call
        ↓
Call 结束 → POST capture
        ↓
自动 Cleanup
        ↓
Packet / PCM / Media Analyzer
        ↓
Deterministic Diagnosis
        ↓
真实 Reasoning Gateway → AI SHADOW
        ↓
Diagnosis Report
        ↓
Golden Candidate 自动评估
        ↓
make m7-acceptance M7_CASE=<case>
```

现场人员不需要为了系统点“开始复现”。当系统判断需要复现时，应由现有自动复现能力直接进入 ARMED/WATCHING。

---

## 7. AI SHADOW 验收要求

M7 的 AI 部分必须使用真实 Reasoning Gateway；fixture 只能验证 contract，不能替代 M7 的 AI Shadow 实测。

M7 期间保持：

```text
AI_PROMOTION_STAGE=SHADOW
```

AI 可以：

- 提出候选 Hypothesis；
- 引用当前 Case Evidence；
- 说明已知/未知/反证；
- 推荐注册的 Question/Reproduction/Experiment；
- 给出面向人的解释。

AI 不可以：

- 把自身 L5 输出变成正式根因；
- 把 Hypothesis 标成 CONFIRMED；
- 产生可直接执行的 SSH/AIM/raw command；
- 引用其他 Case 的 Evidence 作为当前 Case L1/L2 根因证据；
- 修改 Deterministic Diagnosis baseline。

---

## 8. 当前阶段不做的事情

M7 期间暂缓：

- 历史 Case 批量 Backfill（当前无真实存量 Case）；
- 为凑样本制造假的 `GOLDEN_READY`；
- `CONTROLLED_PLANNER` 生产启用；
- 大规模 Prompt/RAG 调参；
- 为未出现的问题继续横向增加大量规则；
- 用临时硬编码 DUT 脚本代替正常系统流程。

---

## 9. 验收结果解释

### PASS

20/20 全部通过，说明**这个 Case 的真实 DUT 智能诊断闭环完整**。

之后可以继续跑下一实验场景，但仍不能据此宣称 AI 模型质量已通过生产门槛。

### BLOCKED

Gate 会输出具体 `blocked_ids` 和每项 remediation。

处理原则：只修对应闭环缺口，然后重跑同一 Case 或新 Case；不要为了让 Gate 变绿去手工伪造数据库记录。

---

## 10. M7 完成后的下一阶段

当至少完成一轮 C01～C06，并确认真实系统闭环稳定后：

1. 正常投入真实问题试用；
2. 让 Golden Candidate 自动积累；
3. 到 10 个 `GOLDEN_READY` 后进行第一次真实 Model Eval；
4. 20～30 个真实 Case 后重新评估 SHADOW → SUGGEST；
5. 只有 `ai-promotion-gate-v1` PASS 后才考虑 CONTROLLED_PLANNER。

因此 M7 的最终产出不是一个“漂亮准确率”，而是一条**真实、可重复、可审计、无设备残留的 VOIP AI 排障闭环**。

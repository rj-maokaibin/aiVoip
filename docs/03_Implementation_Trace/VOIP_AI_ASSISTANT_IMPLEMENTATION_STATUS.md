# VOIP AI 故障助手——实现状态与 PRD/SPEC/代码追踪（Living Document）

> 文档属性：**持续维护 / Single Source of Truth（实现状态）**  
> 首次建立日期：2026-08-28  
> 当前审计基线分支：`master`  
> 当前审计基线 Commit：`001851241ea28c6093e7d994715b1c40a0f07121`  
> 当前总体状态：**Functional RC / Acceptance Closure，Production NOT READY**  
> 维护要求：本文件必须随 PRD、SPEC、核心实现、Acceptance Evidence、Golden Gate、Production Enablement 状态变化同步更新。

---

## 1. 文档目的

本文件用于持续回答以下问题：

1. VOIP AI 故障助手当前到底实现到什么程度；
2. 冻结版 PRD / SPEC 中的能力是否真实落入代码；
3. 哪些能力已经通过单测、集成验收或真实 DUT Gate；
4. 哪些能力只是“有代码”，但还不能视为 Production Ready；
5. 当前真正阻塞发布的问题是什么；
6. 下一阶段最值得投入的能力缺口是什么；
7. 后续代码、PRD、SPEC 或验收状态改变时，需要同步修改哪些结论。

本文件不是以“代码文件数量”判断完成度，也不以单个测试 PASS 直接推导 Production Ready。项目最终状态必须同时结合：

- Frozen PRD；
- SPEC / Engineering Contract；
- PRD/SPEC ↔ Code Traceability；
- 当前 `master` 代码；
- Acceptance Evidence；
- Real DUT Gate；
- Full Backend / Migration / Frontend Security Gate；
- Golden Gate；
- Production Enablement Gate。

---

## 2. 当前总体结论

截至当前基线，VOIP AI 故障助手已经从“原型/分析脚本”阶段进入**核心功能基本实现完成、Release Candidate 后期验收**阶段。

当前可以确认：

- Evidence-first 架构已建立；
- Case / Session / Evidence Bundle 已形成主数据链；
- DUT SSH 自动执行与采证能力已落地；
- SIP / SDP / RTP 分析能力已基本落地；
- PCM 采集与基础分析框架已落地；
- Rule Engine 已建立 deterministic reason code + evidence ID 路径；
- Report 已升级为 Evidence-driven report；
- M6.2 自动复现 / Capture V2 已完成关键真实 DUT Gate 闭环；
- Resource BUSY、hard timeout、cancel race、stop prompt-return 等可靠性问题已完成关键验收；
- M7 AI Intelligence 严格功能 Acceptance 已达到 20/20 PASS；
- DB Migration / Startup Gate 当前通过；
- Frontend Security Gate 当前通过。

但当前仍不能定义为 Production Ready，因为严格 Release Audit 仍显示：

```text
GOLDEN_READY=false
AI_PROMOTION_ELIGIBLE=false
WS3_ENABLEMENT_ELIGIBLE=false
```

并且当前仍存在：

- Full Backend Release Evidence Integrity blocker；
- Golden #00 HOLD / NOT READY。

因此当前正式定性为：

> **核心平台与主要自动诊断链基本落地，已进入 RC/最终验收阶段；功能工程完成度约 90%～95%（工程估算），但 Production Release Gate 尚未通过。**

注意：90%～95% 为基于 PRD/SPEC 与当前代码覆盖的工程估算，不是官方 Release Gate。Production Ready 是二值判断，目前仍为 **NOT READY**。

---

## 3. 当前规格基线与工程原则

### 3.1 Frozen PRD

当前最高层规格为 Frozen PRD（V3.3F 系列基线）。其核心约束不是单纯增加功能，而是冻结以下工程原则：

- Evidence-driven；
- 无证据不下结论；
- stale / incomplete evidence 不得被当作当前事实；
- critical path fail-closed；
- tenant / RBAC 边界；
- SSH trust；
- 敏感数据不得进入不受控日志；
- 诊断结果必须可审计；
- Production Security 必须经过硬 Gate；
- 不允许仅凭“代码存在”或“某个测试绿”宣布实现完成。

因此本项目必须区分：

```text
Implemented
Accepted
Real-DUT Accepted
Release Evidence Complete
Golden Ready
Production Ready
```

这些状态不能互相替代。

### 3.2 主要 SPEC / Contract / Evidence 基线

当前实现与验收主要由以下材料共同约束：

- Implementation Plan；
- Engineering Contract；
- PRD/SPEC ↔ Code Traceability；
- M6.2 Reproduction Intelligence SPEC；
- Capture Engine V2.1 / V2.1.1 SPEC；
- Capture V2 Real Gates；
- Acceptance Infrastructure V2；
- AI Intelligence Implementation / Acceptance；
- Production Enablement Runbook；
- Production Gate Frozen；
- Golden #00 Evidence。

本文件后续更新时，应优先使用**最新有效 Evidence**覆盖旧结论，避免旧的 HOLD / PASS 材料与当前状态混淆。

---

## 4. 里程碑实现状态

| 阶段 / 能力 | 当前判断 | 说明 |
|---|---|---|
| 基础设备接入 / Session | ✅ 基本完成 | SSH、设备状态、Session / Evidence 基线已建立 |
| Evidence Bundle | ✅ 完成度高 | 已成为系统核心数据模型与审计基础 |
| SIP / SDP 分析 | ✅ 基本完成 | SIP parser、dialog/call、method/status、SDP/codec 等已实现 |
| RTP 分析 | ✅ 基本完成 | SSRC、sequence、loss、jitter、方向/QoS 等已有实现 |
| PCM 采集 | ✅ 基本完成 | PCM RX/TX 采集链路与无数据语义已建立 |
| PCM 深度诊断 | ⚠️ 部分完成 | 基础分析可用，复杂音质/电气类专家算法仍需增强 |
| Rule Engine | ✅ 基本完成 | stable reason_code + evidence_id + deterministic mapping |
| Evidence Report | ✅ 基本完成 | Evidence-driven，不再只是自由文本报告 |
| M6.1 Evidence Summary | ✅ 基本完成 | Evidence Summary / Final Evidence Bundle 已进入验收体系 |
| M6.2 自动复现 | ✅ 关键链路完成 | Orchestrator、资源、超时、取消、恢复、审计已落地 |
| Capture V2 Real Gates | ✅ R05-R08 已闭环 | 后续真实 DUT Acceptance 已覆盖早期 HOLD |
| M7 AI Intelligence | ✅ 20/20 PASS | AI 3/3、Legacy fallback 17/17、Policy violation 0/20 |
| Full Backend Test Suite | ✅ 17/17 PASS | 测试套件绿 |
| Full Backend Release Evidence | ⚠️ 未最终通过 | 严格 Audit 仍存在 negative-path middleware contract blocker |
| DB Migration / Startup | ✅ PASS | 最新严格 Audit 判定通过 |
| Frontend Security | ✅ PASS | 最新严格 Audit 判定通过 |
| Golden #00 | ❌ HOLD | 当前仍为 NOT READY |
| Production Enablement | ❌ BLOCKED | 受 Golden / Promotion / WS3 Gate 阻塞 |

---

## 5. M6.2 / Capture V2 当前真实状态

### 5.1 早期状态与当前状态必须区分

Capture V2 / M6.2 早期真实 DUT 验收曾给出 `NOT DONE / HOLD`，当时存在四个关键 P0 blocker：

- R05：Stop 后不能保证 prompt-fast-return；
- R06：资源冲突缺少冻结的 BUSY typed semantics；
- R07：hard timeout 边界不完整；
- R08：cancel race 无法保证 single-terminal truth。

该结论已被后续真实 DUT Acceptance 覆盖，不能继续用早期 HOLD 表示当前 M6.2 状态。

### 5.2 当前闭环结果

#### R05 — Stop

当前已收敛为：

- stop prompt 快速返回；
- 不因底层 capture process 悬挂导致 API 长时间阻塞；
- terminal state 由上层控制面确定。

#### R06 — 资源冲突

已经形成 typed semantics：

```text
RESOURCE_BUSY / BUSY
```

上层 Orchestrator / AI 可以确定性处理资源竞争，而不是依赖模糊字符串错误。

#### R07 — Hard Timeout

当前已冻结 timeout 语义，例如：

```text
TIMEOUT / hard_timeout_stop
```

Timeout 已成为 Session 状态机的一部分，而不是“subprocess 可能卡住”的实现细节。

#### R08 — Cancel Race

当前已实现 single-terminal truth，禁止互斥终态重复出现，例如：

```text
READY → CANCELLED
```

而不是：

```text
CANCELLED → DONE
```

### 5.3 当前 M6.2 结论

> **M6.2 / Capture V2 已不再是当前主要 Release blocker。**

后续除发现新的真实 DUT 回归外，不建议再次进行大规模 Capture 架构重写。

---

## 6. Reproduction / Capture 代码落地情况

当前 `master` 中 Reproduction 已形成明确模块边界，包括：

```text
backend/app/reproduction/
├── orchestrator.py
├── models.py
├── capability.py
├── resources.py
├── safety.py
├── restoration.py
├── persistence.py
└── audit.py
```

并存在 Capture V2 主实现：

```text
backend/app/capture_v2.py
```

其实现已经明显超过“SSH + tcpdump + sleep + killall”的脚本式采集方式。

### 6.1 Session 生命周期

复现过程已经按 Session 管理，而不是简单命令序列。核心语义覆盖：

- prepare；
- resource acquire；
- armed / watching；
- capture；
- target / trigger；
- stopping；
- timeout；
- cancelled；
- failed；
- completed；
- cleanup / restoration。

该方向与 M6.2 设计目标一致：

```text
自动 SSH DUT
→ 提前进入 ARMED / WATCHING
→ 开启轻量采集
→ 等待现场自然复现
→ 识别通话/目标事件
→ Call 后短观察
→ 停止采集
→ cleanup
→ evidence bundle
→ analysis
```

### 6.2 Resource Arbitration

当前已经把 DUT 上的 capture / PCM / debug 等资源作为受控资源处理。

目标是防止：

- 两个 Session 同时占用 tcpdump；
- PCM 通道重复开启；
- debug 状态互相覆盖；
- 一个 Session 清理另一个 Session 的资源。

资源竞争通过 BUSY 类型语义返回，是从实验脚本向工程平台升级的重要标志。

### 6.3 Cleanup / Restoration

当前已有独立 Restoration 能力，系统需要对临时 mutation 建立 Restore Plan，并在：

- success；
- error；
- cancel；
- timeout；

等路径都执行 cleanup / restoration / resource release。

这一能力直接对应现场安全要求：

> PCM、debug、tcpdump 等临时诊断状态不能在分析结束后残留在 DUT 上。

---

## 7. SIP / RTP / PCM 分析实现程度

### 7.1 SIP / SDP

当前能力已覆盖或基本覆盖：

- SIP 报文解析；
- method / status；
- dialog / call 关联；
- SDP；
- codec negotiation；
- signaling 时间关系；
- 标准化 Evidence 输出。

系统方向不是简单 grep `INVITE`，而是：

```text
PCAP → Parser → Normalized Evidence → Rule / AI
```

### 7.2 RTP

当前已覆盖：

- SSRC；
- sequence；
- loss；
- jitter；
- stream direction；
- QoS / media evidence。

因此对以下问题已经具备较好的自动分析基础：

- 单通；
- RTP 无数据；
- 丢包；
- jitter；
- 卡顿；
- SDP/RTP 不匹配；
- codec / media path 问题。

### 7.3 PCM

当前 PCM 能力已经具备：

- PCM 采集；
- 基础 analyzer；
- 有数据 / 无数据判定；
- `ENODATA` 等明确语义；
- analyzer error 降级，不应简单转化为 HTTP 500；
- 与 RTP Evidence 联合分析的基础。

但必须区分“PCM 基础能力”和“音频专家能力”。当前尚不能认为以下复杂问题已全部自动化：

- DTMF 双音频谱完整识别；
- 杂散频率与 DTMF 主频能量差；
- 电流音 / 电源噪声分类；
- handset / speaker A/B；
- AP 电路 / 电话电路边界；
- echo 路径；
- 半双工 panel phone；
- DSP gain / algorithm 前后 PCM 因果分析。

因此 PCM 结论为：

> **采集链与 analyzer 框架成熟度较高，但复杂音质与模拟电气问题的专家知识仍需持续增强。**

---

## 8. Rule Engine 与 AI Intelligence

### 8.1 当前架构方向

当前系统已经避免把所有判断交给 LLM，其核心方向是：

```text
Evidence
   ↓
Deterministic Analyzer
   ↓
Reason Code
   ↓
Rule Engine
   ↓
Diagnosis Candidate
   ↓
AI Explanation / Synthesis
```

而不是：

```text
PCAP → LLM → Guess
```

这符合 Frozen PRD 的 Evidence-first 原则。

### 8.2 Rule Engine 当前实现

当前已有：

- stable reason_code；
- evidence IDs；
- deterministic mapping；
- physical / SIP / RTP / PCM 等分类基础；
- 可审计的 diagnosis path。

因此“为什么判断为某问题”可以回溯到 Evidence，而不是只依赖模型自然语言。

### 8.3 M7 AI Acceptance

当前 M7 严格功能 Acceptance：

```text
Acceptance:       20 / 20 PASS
AI enabled:        3 / 3 PASS
Legacy fallback: 17 / 17 PASS
Policy violation:  0 / 20
```

该结果说明 AI 接入后：

- AI path 可工作；
- legacy deterministic fallback 保持有效；
- 不要求系统“必须有 AI 才能工作”；
- policy enforcement 未发现验收违规。

AI 当前更适合承担：

- Evidence synthesis；
- 多域关联；
- hypothesis ranking；
- 下一步补采建议；
- 人类可读解释；
- Case Copilot。

而 deterministic 层继续负责：

- SIP parser；
- RTP loss / jitter；
- timeout detector；
- Session state machine；
- Rule Engine；
- Release Gate。

---

## 9. 当前最明显的 VOIP 专家知识覆盖缺口

当前平台架构成熟度已经高于 VOIP 领域知识自动化覆盖度。

### 9.1 FXS / SLIC Analyzer 不足

现场人工排障常用证据包括：

- `aimd.s` 是否运行；
- license；
- MAC / license 一致性；
- BBD；
- SPI；
- SLIC init；
- Hook Stage；
- onhook / offhook；
- ring voltage / frequency；
- 国家 / region 参数。

当前这些能力尚未形成与 SIP Analyzer 同等级别的系统化 `FxsAnalyzer`。

建议形成结构化 reason code：

```text
FXS_SERVICE_NOT_RUNNING
LICENSE_INVALID
LICENSE_MAC_MISMATCH
BBD_MISSING
SLIC_INIT_FAILED
SPI_COMMUNICATION_FAILED
HOOK_STATE_MISMATCH
RING_PROFILE_MISMATCH
```

### 9.2 VOIP Config Analyzer 不足

建议将以下人工命令/日志转成结构化 Evidence：

```text
/tmp/voip_log.txt
aim voip sip regc show config RC1
sys show bind-if
aim voip sip regc show running RC1
```

自动判断：

- 配置是否从管理层读出；
- 是否正确下发 aimd；
- account / server / transport / codec 是否一致；
- bind-if 是否正确；
- running config 与 intended config 是否一致。

### 9.3 Voice Network Analyzer 不足

建议系统化覆盖：

```text
/etc/config/network
/etc/config/vlan_ref
brctl show
route
/tmp/voip/resolv.conf
/tmp/networkapi/networkvoip.log
```

并建立因果链：

```text
Config
  ↓
Voice VLAN exists
  ↓
IP acquired
  ↓
Bind Interface
  ↓
Route
  ↓
DNS
  ↓
PBX Reachability
  ↓
REGISTER
```

### 9.4 PBX / Carrier 观测盲区

当前系统控制边界明确为：

> 仅 SSH 控制被测 VOIP DUT，不控制 PBX / Voice Gateway。

因此对以下问题通常只能做“边界归因”：

- PBX firewall；
- blacklist；
- security policy；
- PBX route；
- PBX codec / transcoding；
- PBX DTMF；
- operator trunk；
- carrier signaling/media。

在没有 PBX Evidence 时，系统不应输出：

> “PBX 配置错误。”

更符合 Evidence-first 的表达应为：

> DUT 已完成本端动作，但响应/媒体异常发生在 DUT 边界之外；当前 Evidence 无法直接证明 PBX 内部根因，需要 PBX/上游 Evidence 才能继续收敛。

### 9.5 Audio Path Analyzer 不足

建议建立完整音频路径模型：

```text
Phone
 ↓
SLIC
 ↓
PCM RX
 ↓
DSP
 ↓
RTP TX
 ↓
Network / PBX
 ↓
RTP RX
 ↓
DSP
 ↓
PCM TX
 ↓
SLIC
 ↓
Phone
```

每份 Evidence 应能标记位于哪个节点/边界，以支持回答：

- 噪音在哪一段引入；
- 单通在哪一段断开；
- DTMF 在哪个边界丢失；
- PCM 正常而 RTP 异常，还是 RTP 正常而模拟端异常。

---

## 10. PRD 能力覆盖矩阵

> 星级为工程成熟度评估，不等同 Release Gate。

| PRD 能力域 | 当前成熟度 | 当前评价 |
|---|---:|---|
| Evidence-first diagnosis | ★★★★★ | 核心架构已建立 |
| Case / Session | ★★★★★ | 已实现 |
| Evidence Bundle | ★★★★★ | 主数据模型成熟 |
| SSH DUT execution | ★★★★☆ | 已实现并有安全约束 |
| Device status | ★★★★☆ | 已实现 |
| PCAP capture | ★★★★★ | Capture V2 关键真实 Gate 已通过 |
| SIP analysis | ★★★★☆ | 成熟度较高 |
| RTP analysis | ★★★★☆ | loss/jitter/SSRC 等已覆盖 |
| PCM capture | ★★★★☆ | 已实现 |
| PCM audio diagnosis | ★★★☆☆ | 框架成熟，领域算法需增强 |
| Rule Engine | ★★★★☆ | reason_code/evidence 已建立 |
| Automated reproduction | ★★★★☆ | M6.2 核心链路已实现 |
| Resource arbitration | ★★★★★ | BUSY 等语义已闭环 |
| Timeout / Cancel | ★★★★★ | 关键真实 DUT Gate 已覆盖 |
| Cleanup / Restoration | ★★★★★ | 已成为一等能力 |
| Report | ★★★★☆ | Evidence-driven |
| AI Intelligence | ★★★★☆ | M7 20/20 PASS |
| FXS / SLIC diagnosis | ★★☆☆☆ | 自动规则覆盖不足 |
| VOIP config diagnosis | ★★★☆☆ | 可采证，需加强专用 Analyzer |
| Network causal analysis | ★★★☆☆ | 可采证，归因图谱仍需加强 |
| PBX diagnosis | ★★☆☆☆ | 受观测边界限制 |
| Carrier diagnosis | ★★☆☆☆ | 主要依赖边界归因 |
| Audio electrical diagnosis | ★★☆☆☆ | 仍大量依赖人工 A/B |
| Production security | ★★★★☆ | 接近完成 |
| Golden production gate | ★★☆☆☆ | 当前 HOLD |

---

## 11. 按故障类型评估当前产品实战能力

### 11.1 SIP / RTP / 网络媒体类

当前成熟度最高，已较接近自动处理，包括：

- 注册失败；
- INVITE 无响应；
- SIP timeout；
- codec mismatch；
- RTP 单向；
- RTP loss；
- jitter；
- RTP 无数据；
- SDP/RTP 不匹配；
- 网络媒体类卡顿。

### 11.2 需要现场复现才能定位的问题

M6.2 之后已经具备完整得多的自动链路：

```text
自动进入 DUT
→ prepare
→ acquire resources
→ capture / arm / watch
→ wait for target
→ collect
→ timeout / cancel handling
→ cleanup
→ evidence bundle
→ analysis
```

因此偶现问题已从“架构设计能力”进入“真实实现能力”。

### 11.3 硬件 / 模拟音频 / 复杂 PBX 类

当前仍需要较多人工专家参与，例如：

- FXS 无馈电；
- SLIC / SPI / BBD；
- Ring voltage；
- handset hardware；
- 电源杂音；
- 半双工面板；
- PBX 防火墙/黑名单/私有策略；
- 运营商线路。

这部分是下一阶段提升“VOIP 专家能力”的重点。

---

## 12. 当前 Release Gate 真实状态

### 12.1 Full Backend

当前 Full Backend Acceptance suite 已有：

```text
17 / 17 PASS
```

但最新严格 Release Audit 仍判：

```text
full_backend_pass=false
```

当前 blocker 为 negative-path middleware contract 的 explicit-status exactness / Release Evidence Integrity 问题。

因此必须区分：

```text
Backend tests PASS
≠
Full Backend Production Gate PASS
```

### 12.2 Database / Migration

当前严格 Audit：

```text
database_pass=true
```

### 12.3 Frontend Security

当前严格 Audit：

```text
frontend_security_pass=true
```

### 12.4 Golden #00

当前严格 Audit：

```text
golden_pass=false
```

Golden Evidence 当前仍为：

```text
status=HOLD
final_decision=NOT READY
```

因此最终 Promotion 状态仍为：

```text
GOLDEN_READY=false
AI_PROMOTION_ELIGIBLE=false
WS3_ENABLEMENT_ELIGIBLE=false
```

---

## 13. 当前 Release Path

```text
Frozen PRD
   │
   ↓
Core Implementation
   │
   ↓
M0 ~ M6.1
   │
   ↓
M6.2 Reproduction / Capture V2
   │
   │ Real DUT R05-R08 PASS
   ↓
M7 AI Acceptance
   │
   │ 20/20 PASS
   ↓
Full Backend
   │
   │ Test Suite 17/17 PASS
   │
   ├── Release Evidence Integrity blocker   ← 当前待闭环
   │
DB Migration ───────── PASS
Frontend Security ──── PASS
   │
   ↓
Golden #00
   │
   └── HOLD / NOT READY                     ← 当前待闭环
   ↓
Production Enablement
   │
   └── BLOCKED
```

---

## 14. 当前优先级

### P0：先打穿 Production Gate

当前最高优先级不是继续重构 Capture，而是收敛 Release Gate：

```text
1. 修复 / 收敛 Full Backend negative-path middleware contract
2. rerun Full Backend Acceptance
3. regenerate FULL_BACKEND_ACCEPTANCE_RC_EVIDENCE_V1
4. 确认 integrity_has_blocker=false
5. rerun Golden #00
6. 确认 GOLDEN_READY=true
7. rerun M7 strict audit
8. 确认 AI_PROMOTION_ELIGIBLE=true
9. 确认 WS3_ENABLEMENT_ELIGIBLE=true
```

### P1：把人工 VOIP 排障经验编码成 Domain Analyzer

Production Gate 清零后，下一阶段建议优先实现：

```text
FxsAnalyzer
VoipConfigAnalyzer
VoiceNetworkAnalyzer
AimdAnalyzer
AudioPathAnalyzer
```

目标是把项目从：

> “Evidence 采集 + PCAP/PCM 分析平台”

升级为：

> “接近资深 VOIP 研发工程师的自动排障助手”。

---

## 15. 当前工程成熟度评分

> 以下评分为工程评估，不是官方验收数字。

| 维度 | 当前评价 |
|---|---:|
| 架构完整度 | 9/10 |
| Evidence 体系 | 9/10 |
| 自动采集 | 9/10 |
| M6.2 复现可靠性 | 8.5/10 |
| SIP / RTP | 8.5/10 |
| PCM 基础能力 | 7.5/10 |
| Rule Engine 基础 | 8/10 |
| AI Intelligence | 8/10 |
| VOIP 领域知识自动化 | 6～7/10 |
| FXS / SLIC | 约 5/10 |
| PBX 深度诊断 | 5/10 以下 |
| Production 工程 | 8/10 |
| Production Release | **NOT READY** |

---

## 16. 当前最重要的三个结论

### 结论 1：M6.2 已不再是当前主要风险

Capture V2 的 R05 / R06 / R07 / R08 已被后续真实 DUT 验收关闭。除非出现新的真实回归，不应再把大规模重构 Capture 作为当前 P0。

### 结论 2：当前真正阻挡上线的是 Release Evidence / Golden

即使当前已经存在：

- M7 20/20；
- Backend test 17/17；
- DB PASS；
- Frontend Security PASS；

只要：

```text
full_backend_pass=false
golden_pass=false
GOLDEN_READY=false
```

就不能宣布 Production Ready。

### 结论 3：Production Gate 后的核心工作应转向专家知识覆盖

下一阶段重点不是再建立新的 Session / Evidence / Orchestrator 框架，而是系统化实现：

```text
FXS/SLIC
  ↓
VOIP Config
  ↓
Voice Network
  ↓
SIP
  ↓
RTP
  ↓
PCM
  ↓
Audio Path
  ↓
PBX / Carrier Boundary
```

形成完整 Diagnosis Graph。

---

## 17. 文档同步更新规则（强制维护约定）

本文件定义为 VOIP AI 助手**实现状态单一事实入口**。后续发生以下任一变化时，必须同步更新本文件。

### 17.1 必须触发更新的变更

1. **PRD 变更**
   - 功能范围新增 / 删除；
   - 产品边界变化；
   - Evidence / Security / Release 原则变化。

2. **SPEC / Engineering Contract 变更**
   - 状态机；
   - typed error；
   - Capture 行为；
   - Resource Arbiter；
   - timeout/cancel；
   - cleanup/restoration；
   - Analyzer / Rule 行为。

3. **核心代码变更**
   - Reproduction / Capture；
   - SIP / RTP / PCM Analyzer；
   - Rule Engine；
   - AI Intelligence；
   - Report；
   - Security；
   - DB / Migration；
   - Frontend Gate；
   - Feishu Gateway / Case Gateway。

4. **Acceptance / Evidence 变化**
   - PASS → FAIL / HOLD；
   - HOLD → PASS；
   - 新 Evidence 覆盖旧 Evidence；
   - Real DUT Gate 结果变化。

5. **Release Gate 变化**
   - `full_backend_pass`；
   - `database_pass`；
   - `frontend_security_pass`；
   - `golden_pass`；
   - `GOLDEN_READY`；
   - `AI_PROMOTION_ELIGIBLE`；
   - `WS3_ENABLEMENT_ELIGIBLE`。

6. **专家能力新增**
   - 新增 FxsAnalyzer / VoipConfigAnalyzer / VoiceNetworkAnalyzer / AimdAnalyzer / AudioPathAnalyzer；
   - 人工排障规则被正式转成 deterministic rule；
   - 新 reason code / evidence schema。

### 17.2 更新原则

每次更新至少同步修改：

- 顶部 `当前审计基线 Commit`；
- `当前总体状态`；
- 里程碑实现状态表；
- PRD 能力覆盖矩阵；
- Release Gate 真实状态；
- 当前优先级；
- 已知缺口；
- 文末变更记录。

### 17.3 同 PR 更新要求

原则上，若某个 PR 会改变本文件中的任何结论，则该 PR 应同时更新本文件，避免：

```text
代码已经改变
但实施状态文档仍停留在旧结论
```

若因真实 DUT / 外部验收证据只能在 merge 后生成，则必须在 Evidence 生成后的下一次文档提交中立即补齐。

### 17.4 新证据覆盖旧证据

同一 Gate 存在多个历史结果时：

- 不删除历史；
- 当前结论以最新且有效的 Evidence 为准；
- 文档必须注明旧结果已被哪个后续验收覆盖；
- 不允许把旧 HOLD 与新 PASS 并列后不说明当前有效状态。

---

## 18. 变更记录

### 2026-08-28 — 初版

基于当前 Frozen PRD / SPEC / Implementation Trace / `master` 代码 / M6.2 Real DUT Acceptance / M7 Strict Acceptance / Full Backend / DB / Frontend Security / Golden #00 Evidence，首次建立项目实现状态 Living Document。

初始结论：

- 核心功能基本落地；
- M6.2 / Capture V2 已完成关键真实 DUT Gate；
- M7 20/20 PASS；
- DB / Frontend Security PASS；
- Full Backend suite 17/17 PASS，但 Release Evidence Integrity 仍有 blocker；
- Golden #00 HOLD；
- Production NOT READY；
- 下一阶段先关闭 Production Gate，再提升 FXS/SLIC、VOIP 配置、Voice Network 与 Audio Path 专家能力。

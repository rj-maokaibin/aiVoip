# VOIP AI 故障助手：飞书 AI 诊断整体方案 V1.0

状态：PROPOSED SOLUTION / 2026-08-15  
适用环境：当前开发环境及后续受控试运行  
规格约束：`VOIP_AI_故障助手_SPEC_V1.2_AI诊断与飞书入口修订.md`

## 1. 目标与产品定位

产品默认入口为技服所在飞书群聊中的 `@VOIP AI 故障助手`，以及用户直接私聊机器人。
私聊不要求 @。系统从原消息建立 Case，在同一群聊/私聊会话、同一消息线程中持续更新诊断进度。技服只需要描述现场现象、提供
设备入口或附件，并完成系统无法代替的物理操作；不要求技服理解 PCM、SLIC、aimd、
SIP/RTP 等内部诊断术语。

系统采用“双脑”设计：

- 确定性系统负责事实、协议解析、信号计算、Evidence Gate、状态机、设备动作和安全。
- AI Investigator 负责语义理解、候选假设、跨证据关联、反证检查、下一步建议和解释。

AI 是受控的诊断调查员，不是拥有任意设备权限的自动运维 Agent。任何关键结论必须
引用当前 Case 的 Evidence；任何设备操作必须来自已审核的 Profile/Action Registry。

## 2. 参与角色

| 角色 | 主要职责 |
|---|---|
| 技服 | 描述现象、提供设备/附件、执行必要的现场物理动作、确认修复已实施 |
| 飞书机器人 | Intake、Case 主卡、最小追问、操作通知、结果回传 |
| AI Investigator | Triage、假设、反证、Evidence Gap、下一问题与 Profile 建议、解释 |
| Deterministic Analyzer | SIP/RTP/RTCP/PCM/Audio/FXS/Log 的事实提取和指标计算 |
| Rule/Evidence Gate | 假设状态、证据充分度、确认条件、安全边界 |
| Reproduction Orchestrator | ARM、WATCH、Attempt/Call、Capture、Cleanup、Recovery |
| 技服/研发审核人 | 高风险动作审批、复杂根因确认、Rule/Profile 发布、修复决策 |

## 3. 飞书入口

群聊与私聊能力对等：

- 群聊：用户需要 @机器人，`chat_type=group`。
- 私聊：用户直接发送消息，`chat_type=p2p`，无需 @机器人。
- 两者都使用事件中的 `chat_id`（`oc_*`）绑定 Case 和回复；`sender_open_id` 只标识操作人。
- 飞书应用需同时开通群聊 @ 消息和用户私聊消息的接收权限，并订阅接收消息 v2.0。

### 3.1 推荐输入

技服可以直接使用自然语言：

```text
@VOIP AI助手

APF1250，SN=MACC1JZH3260M。
客户反馈拨号时偶尔首位号码丢失，今天出现三次。
设备入口：https://example.noc.rj.link/...
现在可以配合复现。
```

可附带 PCAP/PCAPNG、设备日志、PCM/WAV、现场录音、页面截图、故障时间、版本、
对端号码或 PBX 信息。系统不得要求用户按照固定表单完整填写后才开始处理；已提供的
自然语言和附件应先被解析，只有真正缺失且系统无法自动获取的信息才追问。

### 3.2 消息意图

AI Intake 至少识别：

- `NEW_DIAGNOSIS`：新故障。
- `CASE_FOLLOW_UP`：补充现象、附件或回答现场问题。
- `STATUS_QUERY`：查询进度。
- `STOP_REPRODUCTION`：请求安全停止。
- `EXTERNAL_ACTION_COMPLETED`：现场动作已完成。
- `FIX_APPLIED`：修复已实施，准备验证。
- `GENERAL_QUESTION`：询问结论、证据或操作说明。

任何文本不得再被无条件解释为“开 SSH 并立即开始复现”。

### 3.3 Case 归属

Case 绑定以下来源信息：

- `chat_id`
- `source_message_id`
- `thread_id/root_message_id`
- `sender_open_id`
- `message_timestamp`

新消息是否关联已有 Case，必须综合线程、显式 Case 编号、设备身份、时间窗口和故障
指纹判断。禁止只按 SN 复用仍打开的 Case，否则同一设备上的不同故障会被错误合并。

## 4. 端到端工作流

```text
群聊 @机器人
  → Feishu Event Normalize / Deduplicate / Permission
  → AI Intake：意图、现象、设备、附件、缺失信息
  → 创建或关联 Case
  → Evidence Precheck：已有附件/历史采集是否可用
  → 基础只读采集或离线 Analyzer
  → Deterministic Reasoner + Rule Engine
  → AI Investigator：候选假设、Evidence 引用、缺口、下一问题
  → AI Contradiction Critic：反证、替代解释、过度断言检查
  → Deterministic Evidence Gate
      ├─ 充分：形成诊断结论和报告
      ├─ 可自动补采：运行低风险 Analyzer/CollectionProfile
      ├─ 需要复现：推荐 ReproductionProfile，经 Policy 校验后自动 ARM
      ├─ 需要物理操作：飞书只询问一个可执行的现场问题
      └─ 无法继续：明确阻塞原因并进入 WAITING_USER
  → Reproduction：FXS_MONITOR_READY 后通知现场操作
  → Attempt/Call/Capture/Analyzer/Cleanup
  → 新 Evidence 进入下一轮诊断
  → Root Cause Gate / Experiment / Fix Verification
  → 飞书结论卡 + Web 深度报告 + Knowledge/Rule Draft
```

## 5. 内部问题与用户问题分离

### 5.1 内部 DiagnosticQuestion

以下问题由系统自动回答，不得要求技服回复：

- PCM 输入数字是否完整？
- SLIC/驱动上报是否完整？
- aimd 号码组装是否完整？
- SIP INVITE 被叫号码是否完整？
- RTP SSRC、方向、Codec、ptime 是否符合预期？
- PCM RX/TX、SIP、RTP 的时间窗口能否对齐？
- 首个确定性 mismatch 位于哪一层？

这些问题保存在 Question DAG 中，由 Evidence 和 Rule Engine 更新状态。

### 5.2 可以询问技服的问题

仅允许询问系统无法观察或执行的内容：

- 问题现在是否还能复现？
- 是每次发生还是偶尔发生？
- 哪一方听不到声音？
- 实际拨打的号码是什么？
- 是否方便现在正常拨号一次？
- 是否可以更换话机、端口或线路做一次对比？
- 升级、回退或配置修改是否已经完成？

每轮最多提出一个现场问题，必须说明具体操作和完成判定。能通过设备、附件或 Analyzer
获得的信息不得转嫁给技服。

### 5.3 用户可见文案示例

```text
Case：VOIP-20260815-0012
现象：偶发首位号码丢失
状态：系统正在自动检查拨号链路

正在自动验证：
- 话机按键是否被设备完整接收
- 号码在哪个处理阶段开始缺失
- 最终发出的呼叫号码是否完整

现场暂时无需操作。
采集准备完成后，机器人会通知您正常摘机拨号。
```

专业字段默认折叠在“查看诊断详情”中。

## 6. 复现协同

### 6.1 是否需要复现

系统先分析已有附件和基础 Evidence。只有当前 DiagnosticQuestion 需要业务活动证据时，
AI 才能建议复现。AI 输出注册的 `profile_id`、理由、预期 Evidence 和可区分的假设，
后端 Policy 决定是否创建 Session。

无法可靠匹配专用 Profile 时可建议 `VOIP_GENERIC_FULL_CAPTURE`，但必须记录回退原因。

### 6.2 现场通知

- ARM 中：`正在准备采集，请暂勿操作话机。`
- 仅 Session `WATCHING`：仍不得提示可以操作。
- 收到 `FXS_MONITOR_READY`：`采集已就绪，请正常摘机拨号；挂断后无需回复。`
- 收到 `FXS_MONITOR_FAILED`：`监听失败，请停止操作；系统正在安全恢复。`
- 无法自动识别物理操作完成时，才显示“现场操作已完成”按钮。

### 6.3 消息策略

普通 Packet、Segment、Attempt、Call 进度只更新 Case 主卡，不刷屏。以下事件允许主动通知：

- 需要现场提供信息或执行动作。
- `FXS_MONITOR_READY` / `FXS_MONITOR_FAILED`。
- Target Finding captured。
- Cleanup Failed / DUT quarantined。
- Root Cause Confirmed。
- Fix Verification 完成。

机器人标准反馈分为六类：已受理、Case 已创建、附件已登记、等待用户、
诊断完成和诊断失败。前三类说明请求已进入哪一步；`WAITING_USER`
每轮只问一个现场问题，并提供“不知道/暂时不能”回复路径；完成时给出
结论摘要并引导查看主卡；失败时明确已停止自动推进。Case 里程碑通知
按 `case_id + feedback_type + token` 幂等，Celery 重试不会重复刷屏。

用户在原线程回答后，系统将文本作为 `USER_RESPONSE` Evidence 持久化，并恢复
对应 Case 的等待中诊断。重复投递的同一条飞书消息只会生成一份 Evidence。

## 7. AI 功能范围

### 7.1 第一批能力

1. `AI-F01 Evidence Quality Auditor`：发现时间窗、方向、Scope、附件和 Analyzer 缺口。
2. `AI-F02 Contradiction Critic`：主动寻找反证、替代解释和过度断言。
3. `AI-F03 Hypothesis Discrimination Planner`：说明下一动作能区分哪些假设。
4. `AI-F04 Confidence Calibration`：展示原始与校准置信度，置信度不作为确认 Gate。
5. `AI-F05 Role-aware Explanation`：现场、技服、研发、客户、教学五种解释。
6. `AI-F06 Experiment Designer`：从审核的 ExperimentProfile 选择 A/B 或 A-B-A。

### 7.2 后续能力

- `AI-F07 Fix Verification Copilot`
- `AI-F08 Version Regression Intelligence`
- `AI-F09 Fleet/Problem Group Detection`
- `AI-F10 Multimodal Field Evidence Understanding`
- `AI-F11 Rule/Profile Copilot`
- `AI-F12 Source/Commit Localization`
- `AI-F13 Knowledge Conflict Detection`
- `AI-F14 Regression Scenario Generation`

AI-F11～F14 只能生成草案，不能自动发布 Rule/Profile、提交代码或替代真实 Field Golden。

## 8. AI 输出合同

AI 必须返回结构化对象，禁止业务代码解析自由文本来决定动作：

```json
{
  "schema_version": "ai-proposal-v1",
  "intent": "DIAGNOSIS_ENHANCEMENT",
  "hypotheses": [
    {
      "code": "DTMF_DIGIT_ASSEMBLY_MISMATCH",
      "title": "号码组装链路候选异常",
      "fault_domain": "DTMF/Call-Control",
      "confidence": 0.68,
      "supporting_evidence_ids": ["evidence-id"],
      "contradicting_evidence_ids": [],
      "missing_evidence": ["AIMD_DIGITS"]
    }
  ],
  "next_question_key": "DTMF_FIRST_MISMATCH_LAYER",
  "recommended_action": {
    "action_type": "SELECT_REPRODUCTION_PROFILE",
    "profile_id": "DTMF_LOSS",
    "reason": "需要对齐 PCM、FXS、aimd 与 SIP 数字链路",
    "distinguishes": ["SLIC_INPUT_LOSS", "AIMD_ASSEMBLY_LOSS", "SIP_TARGET_LOSS"]
  },
  "explanation": "AI建议；最终状态由确定性Evidence Gate决定。"
}
```

后端必须验证：

- Evidence ID 属于当前 Case 且调用者有权限。
- Hypothesis code、Question key、Profile ID、Action type 已注册。
- AI 不得创建 Shell/AIM 命令或修改安全参数。
- AI Hypothesis 默认 `OPEN`、Evidence Level 为 `L5`、不可确认根因。
- AI 建议与确定性事实冲突时保留为 rejected proposal，并记录原因。

## 9. Case 主卡

主卡包含：

- Case 编号、设备别名、现象摘要、当前阶段。
- 当前用户是否需要操作。
- 已确认事实、仍在自动验证、需要用户补充的信息。
- 第一候选方向及结论等级，不展示虚假确定性。
- Reproduction Runtime Ready/Failed、Capture、Cleanup、Fix Verification。
- 查看详情、安全停止、补充附件、现场操作完成、登记修复等受控按钮。

主卡不直接展示 PCM 端口、SSRC、内部状态枚举等实现细节，除非用户展开研发详情。

## 10. 安全、隐私与治理

- AI 不接收 SSH 密码、Token、Secret 或任意设备命令执行能力。
- 外部模型默认不接收 Raw PCAP/PCM/WAV；只发送脱敏结构化摘要。
- 设备 IP、SN 等标识默认使用 alias，按企业策略显式开启后才发送。
- Prompt Injection 不得改变 Action Registry、风险等级、审批和 Cleanup。
- 保存模型、Prompt/Workflow 版本、输入指纹、结构化输出、校验结果、耗时和降级原因。
- LLM 不可用时继续 Analyzer、Rule、Evidence Gate 和确定性报告。
- AI 循环达到 No Progress/Max Cycles 后进入 `WAITING_USER`，不得无限补采。

## 11. 交付路线

### Phase AI-0：可靠性前置

- 孤儿 Session 检测：锁丢失、任务消失、Lease 过期均可恢复。
- watcher 异常必须进入 Recovery/Cleanup。
- 飞书消息幂等、线程归属和 Case 关联修正。

### Phase AI-1：Shadow Mode

- 接入 Reasoning Gateway，但 AI 输出不改变正式诊断。
- 实现 AIProposal Schema、引用校验、审计和 Web 对比。
- 使用现有 Golden/历史 Case 运行离线 Eval。

### Phase AI-2：只读增强

- 开放 Evidence Quality、Contradiction Critic、角色化解释。
- AI 候选保持 `OPEN/L5`，不触发设备操作。

### Phase AI-3：受控规划

- 开放下一 Question、CollectionProfile、ReproductionProfile 推荐。
- 后端 Policy 批准后才能自动执行已有 L0/L1 或已审核 L2 Profile。

### Phase AI-4：知识与研发闭环

- Similar Case RAG、Problem Group、Rule/Profile Draft、Version/Commit 辅助。
- 所有发布和代码变更保持人工审核。

## 12. 验收指标

- 关键 AI 结论 Evidence 引用覆盖率 100%。
- 不存在或跨 Case Evidence 引用接受数为 0。
- AI 单独确认 Root Cause 数为 0。
- 未注册/越权设备动作执行数为 0。
- LLM 故障时确定性诊断成功降级率 100%。
- 普通进度消息不刷屏；一个 Case 默认维护一张主卡。
- 技服无需回答内部协议/PCM/aimd 问题。
- AI 推荐 Question/Profile 的人工接受率、故障域命中率和编造事实率进入版本化 Eval。
- Shadow Mode 达标前不得启用 AI 自动创建 ReproductionSession。

建议目标（需通过真实 Eval 校准，不作为预先承诺）：常见故障 5～15 分钟形成首轮方向，
无效重复采集降低 30%～50%，60%～80% 的常见 Case 自动收敛到明确故障域或故障层。

## 13. 当前实现与目标差距

当前已具备飞书长连接、文本设备参数解析、Case/群绑定、Generic 自动复现、确定性
Analyzer/Reasoner、Rule、Question DAG、Evidence Gate、报告和可选 Hybrid Gateway。

截至 2026-08-16 已完成：

- `AI-0`：ACTIVE 锁租约过期恢复，以及“锁行已丢失、Session 租约已过期”的孤儿恢复；
  飞书 `event_id/message_id` 幂等；来源消息、根消息、父消息、发送人和会话类型持久化；
  Case 不再按 SN 单独复用，只允许同一飞书线程关联。
- `AI-1 基础`：`ai-proposal-v1` Schema、Evidence 跨 Case 校验、注册 Question/Profile
  校验、命令内容拒绝、`OPEN/confirmable=false/L5/confidence<=0.75` 强制约束、Shadow
  审计表、baseline 差异和 Gateway 降级。Shadow 不合并正式 DiagnosisDecision，也不执行动作。
- 开发环境已具备 AI Proposal Shadow 及只读工作台数据合同。`AI_SHADOW_ENABLED` 默认关闭；未配置
  Reasoning Gateway 时不会产生真实模型 Proposal。
- `Intake/Evidence First`：已增加确定性 Intent Router；任意非空文本不再直接开通设备；
  设备信息缺少现象时只追问；附件消息优先通过飞书消息资源接口下载并登记为不可变
  Evidence；“现象+设备”只启动确定性诊断与最小只读采集，不创建 Generic
  ReproductionSession。消息幂等已按飞书建议改为优先使用 `message_id`。
- 飞书来源 `tenant/chat/message/root/parent/sender/timestamp/normalized_text/attachment_refs`
  已随 Case 绑定持久化；AI 推荐接受/拒绝也以 append-only 反馈持久化。
- 群聊 `group` 与机器人私聊 `p2p` 已按同一事件合同处理；私聊支持无需 @ 的文本诊断、
  附件 Evidence First、状态查询、安全停止和结果回传。
- 已实现六类用户可见反馈：已受理、Case 已创建、附件已登记、单问题
  `WAITING_USER`、诊断完成和诊断失败；里程碑回复幂等，状态查询不直接暴露
  内部枚举，用户问题不包含 PCM/SLIC/aimd/SIP/RTP 等内部检查项；用户
  线程回复已能幂等转换为 Evidence 并恢复 `WAITING_USER` 诊断。
- 文本现场操作完成和已修复已分别接入 Experiment/Fix Verification 现有状态机；多个
  等待中实验不会猜测，未确认根因不会启动修复验证。
- Case Correlation 已增加同 chat、24 小时、精确设备身份与具体症状联合指纹；同分
  多候选要求显式 Case 编号，并审计自动关联理由。
- 附件支持部分成功：可用文件继续诊断，失败文件显示文件名并要求原线程重发，
  重复任务不重复登记同一个附件 Evidence。
- `AI-F10` 基础链路已接通：WAV/OGG/Opus 经受限解码后进入波形/频谱/音质候选分析；
  图片完成文件头/尺寸校验和中英文 OCR，敏感行脱敏，字段候选固定为 L4；同一 Case
  同时有 PCAP 时，现场录音与 RTP/PCM Artifact 做确定性信号相关，输出偏移、绝对时间
  和 Call-ID。相关与 OCR 均不能直接确认系统根因。
- `GENERAL_QUESTION` 使用已审核知识库做可追溯回答，答案显示知识条目来源；无匹配时
  明确拒绝猜测，不需要开启 AI Gateway。
- 修复验证计划已能幂等创建并启动专用验证 Session，绑定基线设备/Profile；Session
  完成且 Cleanup Verified 后，系统自动关联最新已分析 Call 并调用现有 FixVerification
  Evidence Gate，证据不足则保持延后。

开发环境新增完成：

- AI-F01/F02/F03/F05 只读工作台、Shadow/Diff Web API、五角色解释和受控
  Question/Profile 建议。
- 19 类 AI Shadow/Safety/Degradation Eval Golden、五个硬零指标、建议接受率反馈和
  不自动开启动作的 Eval Gate。
- Raw PCM 显式参数导入、截图几何/颜色 L4 候选、多模型 Gateway failover 与外发脱敏。
- 版本回归/问题组/知识冲突候选，以及 Rule/Profile/Knowledge/Code/Regression 不可执行 DRAFT。

剩余项均是外部效果验收：真实飞书租户、真实现场附件/设备、真实历史 Case 和真实
Reasoning Gateway 的效果、延迟与成本。在这些 Gate 通过前，产品仍不宣称 AI 可自主确认根因或执行动作。

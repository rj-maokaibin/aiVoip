# VOIP AI 故障助手 SPEC V1.2：AI 诊断与飞书入口修订

状态：DEVELOPMENT IMPLEMENTED / 2026-08-16  
基线：`VOIP_AI_故障助手_SPEC_V1.0_终稿.docx`  
兼容修订：`VOIP_AI_故障助手_SPEC_V1.1_复现采集架构修订.md`  
配套方案：`VOIP_AI_故障助手_飞书AI诊断整体方案_V1.0.md`

## 1. 范围与优先级

本修订定义飞书群聊与机器人私聊入口、AI Intake、AI Evidence Reasoning、用户交互、Shadow Eval、
受控规划和 AI 治理。V1.0 中未被替代的条款继续有效；V1.1 仍是 Reproduction Capture、
Readiness、Signal Observer、Cleanup 和 Evidence Locator 的权威规格。

发生冲突时：

1. 设备动作、复现状态、Capture/Cleanup 以 V1.1 和 Engineering Contract 为准。
2. 飞书 Intake、AI Proposal、用户问答和模型治理以本修订为准。
3. 不得以 AI 建议覆盖确定性事实、Evidence Gate 或安全策略。

本修订中的 MUST/必须为验收强约束，SHOULD/应为默认行为，MAY/可为扩展行为。

## 2. FEISHU-AI-001｜唯一默认入口与会话归属

默认用户入口包括技服飞书群聊中的 `@机器人` 消息，以及用户直接发送给机器人的私聊
消息。私聊不要求 `@机器人`。HTTP Callback 与 WebSocket Long Connection 必须归一化为
同一事件合同，且群聊与私聊具备相同的 Intake、附件、状态查询、停止和结果回传能力。

飞书应用必须订阅“接收消息 v2.0”，并同时具备群聊 @ 消息与用户私聊消息的接收权限。
私聊事件的 `chat_type=p2p`，系统必须使用事件中的会话 `chat_id`（`oc_*`）作为 Case
绑定和回复目标，不得把发送人的 `open_id` 当作会话 `chat_id`。

系统必须保存：

```text
tenant_key / chat_id / chat_type / source_message_id / root_message_id
sender_open_id / message_timestamp / event_id / normalized_text / attachment_refs
```

一个 Case 默认在原消息线程维护一张主卡。普通状态变化更新主卡，不发送新消息。

事件必须按 Feishu event/message ID 幂等。重复投递不得重复创建 Case、开 SSH、创建
ReproductionSession 或执行动作。

## 3. FEISHU-AI-002｜Intent Router

任何非空文本不得无条件进入设备开通或自动复现。系统必须先分类以下意图：

```text
NEW_DIAGNOSIS / CASE_FOLLOW_UP / STATUS_QUERY / STOP_REPRODUCTION
EXTERNAL_ACTION_COMPLETED / FIX_APPLIED / GENERAL_QUESTION / UNSUPPORTED
```

Intent Router 输出至少包含：

```json
{
  "intent": "NEW_DIAGNOSIS",
  "confidence": 0.0,
  "case_ref": null,
  "device_refs": [],
  "symptoms": [],
  "attachments": [],
  "missing_user_inputs": [],
  "requires_device_access": false
}
```

低置信度、多个 Case 候选或动作语义不明确时，系统必须追问，不得猜测用户意图。

`STOP_REPRODUCTION` 和卡片安全停止按钮必须走 `reproduction-control-high`，不依赖
AI 模型在线。

## 4. FEISHU-AI-003｜Case Correlation

Case 关联必须依次考虑：

1. 显式 Case 编号。
2. 同一 root message/thread 的活动 Case。
3. 同一 chat + device + symptom fingerprint + 时间窗口。
4. 无可靠匹配时创建新 Case。

禁止仅根据设备 SN 复用任意 `NEW/ANALYZING` Case。相同设备上的不同故障必须可并行
保留独立 Case；设备诊断动作仍受物理 DUT 级锁约束。

Case Correlation 的候选、得分、最终选择和理由必须审计。

## 5. FEISHU-AI-004｜最小用户追问

用户问题只允许包含系统无法自动观察或执行的信息，例如：

- 现象能否复现、发生频率、故障时间。
- 哪一方无声、实际拨号号码、客户感知。
- 是否可以现在拨号、换话机/端口/线路。
- 外部物理动作或修复是否完成。

以下内容属于内部 DiagnosticQuestion，不得要求技服回答：

- PCM/SLIC/aimd/SIP 数字是否完整。
- SIP/RTP 字段、SSRC、Codec、ptime、方向和时间对齐。
- Capture Channel、Analyzer、Evidence Level、mismatch layer 等内部事实。

系统每轮最多提出一个用户问题，必须提供具体动作、完成条件和“不知道/暂时不能操作”
路径。能从设备、附件、Analyzer 或历史当前 Case Evidence 获取的信息不得转嫁给用户。

用户在原线程中的回复必须按 `source_message_id` 幂等登记为 append-only
`USER_RESPONSE` Evidence，保留原消息、root message 和发送人上下文，然后只恢复
已关联 Case 的等待中诊断。不得仅回复“已关联”而不持久化、不恢复工作流。

## 6. FEISHU-AI-005｜Evidence First 诊断顺序

新 Case 必须按以下顺序处理：

1. Intake 和附件登记。
2. 已有 Evidence Precheck。
3. 离线 Analyzer 或审核过的最小只读 CollectionProfile。
4. Deterministic Reasoner + Rule Engine。
5. 可选 AI Investigator/Contradiction Critic。
6. Deterministic Evidence Sufficiency Gate。
7. 仅在仍需真实业务活动证据时考虑 Reproduction。

不得因为用户提供设备 URL/IP/SN 就无条件创建 Generic ReproductionSession。已有附件
足够回答当前 Question 时应先分析附件。

## 7. AI-DIAG-001｜双脑执行模型

系统必须保持以下职责边界：

| 能力 | 权威执行者 |
|---|---|
| SIP/RTP/RTCP/PCM/Audio/FXS/Log 事实 | Deterministic Analyzer |
| 时间映射、Call Binding、Capture Health | Deterministic Runtime |
| Evidence Sufficiency、Confirm Rule、Contradiction Gate | Rule/Gate |
| 设备 Action、ARM、Cleanup、Recovery | Profile/Orchestrator |
| 语义理解、候选假设、关联、解释、下一步建议 | AI Investigator |

AI 输出永远是 Proposal。系统不得以模型自然语言直接修改状态机、数据库核心状态或执行
设备命令。

## 8. AI-DIAG-002｜AIProposal 合同

AI 输出必须通过版本化 Schema 校验。最低合同为：

```json
{
  "schema_version": "ai-proposal-v1",
  "intent": "DIAGNOSIS_ENHANCEMENT",
  "hypotheses": [
    {
      "code": "string",
      "title": "string",
      "fault_domain": "string",
      "confidence": 0.0,
      "rationale": "string",
      "supporting_evidence_ids": [],
      "contradicting_evidence_ids": [],
      "missing_evidence": []
    }
  ],
  "known": [],
  "unknown": [],
  "excluded": [],
  "next_question_key": null,
  "recommended_action": null,
  "user_explanation": "string"
}
```

Validator 必须拒绝：

- 不存在、跨 Case 或无权限 Evidence ID。
- 未注册 Question、Profile、ExperimentProfile 或 Action。
- 任意 Shell/AIM 命令、命令模板或安全参数。
- 将 L4/L5 证据标记为直接证据。
- 将 AI Hypothesis 直接标记为 SUPPORTED/CONFIRMED。
- 与确定性事实冲突且未声明 contradiction 的 Proposal。

通过校验的新增 AI Hypothesis 仍必须为 `OPEN`、`confirmable=false`、Evidence Level=`L5`，
模型置信度上限为 0.75。被拒 Proposal 及拒绝原因必须保存用于 Eval。

## 9. AI-DIAG-003｜Evidence Quality Auditor

AI-F01 必须检查但无权最终裁决：

- 时间窗口是否覆盖用户现象或 Attempt/Call。
- 必需方向、Scope、Capture Point 是否缺失。
- Evidence 是否重复、损坏、部分完成或 Analyzer 未执行。
- 用户现象与当前采集对象是否匹配。
- 当前结论是否引用了不可用或超出精度的 Evidence。

最终 Completeness/Availability/Sufficiency 仍由确定性组件生成。

## 10. AI-DIAG-004｜Contradiction Critic

任何准备展示为第一候选方向的 AI 增强结果必须经过 AI-F02 Critic。Critic 至少输出：

```text
hard_contradictions / soft_contradictions / alternative_explanations
unsupported_claims / missing_discriminating_evidence
```

Critic 不得删除确定性 Hypothesis 或 Evidence。Hard Contradiction 必须交给确定性 Gate
处理；AI 不能自行忽略。

## 11. AI-DIAG-005｜下一问题与假设区分规划

AI-F03 可从已注册 QuestionTemplate 中建议下一问题，但后端必须重新计算资格、父节点、
信息增益、成本和风险。

推荐动作必须说明：

- 能区分哪些候选假设。
- 每个可能结果将支持/弱化什么。
- 需要哪些 Evidence 和 Capture Point。
- 是否需要现场动作、预计耗时和风险。

“再次抓包”“再试一次”等无区分目标的建议不得自动执行。

## 12. AI-DIAG-006｜ReproductionProfile 推荐

AI 只能推荐已注册 `ReproductionProfile.id`，并输出：

```text
profile_id / reason / expected_evidence / distinguishes / user_action_needed
```

后端必须验证当前 DiagnosticQuestion 确实需要业务活动证据、设备能力满足 Profile、没有
Active/Quarantined 冲突、风险与 Cleanup 合同通过。无法可靠选择专用 Profile 时可回退
`VOIP_GENERIC_FULL_CAPTURE`，但必须记录回退原因。

Shadow Mode 或 Eval 未达标时，AI 推荐不得自动创建 ReproductionSession。

## 13. AI-DIAG-007｜复现用户语义

飞书/Web 不得以 `WATCHING` 单独表示可以操作。只有收到并持久化
`FXS_MONITOR_READY` 后才能通知现场正常摘机/拨号。

READY 前：`正在准备采集，请暂勿操作话机。`  
READY 后：`采集已就绪，请正常摘机拨号；挂断后无需回复。`  
FAILED 后：`监听失败，请停止操作；系统正在安全恢复。`

内部 Readiness Phase、PCM 端口和 Debug 实现细节默认不展示给技服。

## 14. AI-DIAG-008｜角色化解释与报告

AI-F05 可从同一 Evidence Snapshot 生成现场、技服、研发、客户和教学版本。所有版本
必须保持事实、结论等级和 Evidence 引用一致。

关键句必须可解析到 Evidence ID；没有引用的模型内容必须显式标记为“AI候选解释”。
报告必须继续记录 Reasoner、Rule、Analyzer、Model、Prompt/Workflow 版本。

AI 报告失败不得阻止确定性 HTML/JSON 报告生成。

## 15. AI-DIAG-009｜Experiment 与 Fix Verification

AI-F06/F07 只能选择审核过的 ExperimentProfile/Fix Verification 合同。建议必须包含：

- 唯一自变量与控制变量。
- Target Finding 和可比环境条件。
- 成功、失败、回归、无结论标准。
- 外部动作、风险、回滚和最大轮次。

因果确认和 `FIX_VERIFIED` 必须由确定性 Causal/Fix Gate 产生。

## 16. AI-KNOW-001｜Similar Case 与知识

AI 可以使用已授权的 Similar Case、Knowledge Item、Rule、SOP 和版本信息。其证据等级：

- Historical/Knowledge 为 L4，只调整 prior、排序和解释。
- 模型推断为 L5，只解释、排序和提出下一步。

相似 Case 输出必须解释相同点和关键差异，不得只提供一个无含义的相似度分数。

AI-F11～F14 生成的 Rule/Profile/Knowledge/Code/Regression Scenario 必须为 DRAFT，经过
人工审核和 Golden/反例测试后才能发布。AI 不得自动提交代码或部署。

## 17. AI-GOV-001｜隐私与数据最小化

外部 Reasoning Gateway 默认只能接收结构化摘要。以下内容禁止默认外发：

- Raw PCAP/PCAPNG、PCM、WAV、视频和原始日志全文。
- SSH 密码、Token、Cookie、Secret、动态凭据。
- 非必要 IP、SN、MAC、电话号码、客户标识。

设备默认使用 alias。任何放宽必须由配置、权限和审计共同控制，不能由 Prompt 或模型
请求动态打开。

## 18. AI-GOV-002｜Shadow Mode、降级与审计

首次接入真实模型必须运行 Shadow Mode：

- 正式结果仍来自 Deterministic Reasoner/Rule。
- AI 使用同一 Evidence Snapshot 生成 Proposal，但不合并、不执行。
- 保存输入 fingerprint、模型、Prompt/Workflow、耗时、输出、Validator 结果和差异。

Gateway 超时、错误、无效 Schema 或策略拒绝时必须降级，不得使 Case 失败。达到
No Progress/Max Cycles 后停止自动循环并进入 `WAITING_USER`。

生产/自动动作权限必须分阶段开启，禁止用单个 `hybrid=true` 同时开放解释、假设、规划
和 Reproduction 自动执行。

## 19. AI-GOV-003｜Eval Gate

AI Eval 至少覆盖：

- REGISTER failure、INVITE failure、one-way audio。
- RTP loss/jitter/stutter、DTMF first digit loss、echo、noise/interference。
- 正常通话负样本、Evidence 不足、PARTIAL/UNAVAILABLE。
- 相似现象不同根因、相同根因不同表象。
- Prompt Injection、伪造 Evidence ID、未注册动作、跨 Case 引用。
- Gateway timeout、invalid JSON、模型版本切换和降级。

必须报告：故障域命中率、候选覆盖率、Evidence 引用正确率、编造事实率、反证发现率、
Question/Profile 推荐接受率、越权建议数、延迟和成本。

以下为硬零指标：

```text
AI_ONLY_ROOT_CAUSE_CONFIRMED = 0
UNREGISTERED_ACTION_EXECUTED = 0
CROSS_CASE_EVIDENCE_ACCEPTED = 0
SECRET_SENT_TO_REASONING_GATEWAY = 0
WATCHING_ONLY_USER_READY_NOTIFICATION = 0
```

## 20. FEISHU-AI-006｜主卡与通知合同

Case 主卡必须展示用户可理解的：现象、当前阶段、是否需要操作、已确认事实、正在自动
验证、第一候选方向、结论等级、Capture/Cleanup/Fix 状态和详情入口。

普通 Analyzer、Packet、Segment、Attempt、Call 更新只刷新主卡。以下事件可主动通知：

- 用户输入/物理动作必需。
- `FXS_MONITOR_READY` / `FXS_MONITOR_FAILED`。
- Target captured。
- Cleanup Failed / DUT quarantined。
- Root Cause Confirmed。
- Fix Verification terminal result。

用户可见反馈必须覆盖以下六个标准节点，群聊和私聊使用同一合同：

1. `ACCEPTED`：即时确认已受理，并说明将先检查现有 Evidence。
2. `CASE_CREATED`：返回 Case 编号，说明已进入诊断。
3. `ATTACHMENT_READY`：确认附件已登记，并明确附件路径暂不启动设备复现。
4. `WAITING_USER`：每轮只询问一个用户可回答的问题，包含“不知道/暂时不能”路径。
5. `COMPLETED`：返回结论摘要，证据和详情保留在 Case 主卡。
6. `FAILED`：说明自动推进已停止、不会执行未确认设备动作，并给出重试/人工处理路径。

`WAITING_USER/COMPLETED/FAILED` 等 Case 里程碑回复必须以
`case_id + feedback_type + milestone_token` 幂等；任务重试不得重复发送。
状态查询必须返回用户可理解的中文阶段，不得直接暴露状态机枚举。

主卡按钮只能携带注册 Action value，不能携带 Shell、命令模板、Secret 或未校验参数。

## 21. 交付顺序

1. `AI-0 Reliability`：孤儿 Session、消息幂等、线程/Case 关联。
2. `AI-1 Shadow`：Proposal Schema、Gateway、Validator、Audit、Eval。
3. `AI-2 Read-only`：Quality、Critic、解释，不改变正式 Hypothesis。
4. `AI-3 Controlled Planning`：Question/Collection/Profile 推荐，经 Policy 执行。
5. `AI-4 Knowledge/Engineering`：RAG、Problem Group、Rule/Profile/Code Draft。

任何后续阶段不得掩盖前序阶段的可靠性、安全或 Eval 失败。

## 22. 当前实现差距（非合同降级）

截至 2026-08-16，现有代码已经具备飞书长连接、设备请求解析、Case 绑定、Card、确定性
Diagnosis/Rule/Question/Evidence Gate、Generic 自动复现和 Hybrid Gateway 基础。

本修订已落地的增量包括：

- AI-0：无 ACTIVE 锁且 Session 租约过期的孤儿恢复、飞书消息幂等、来源上下文持久化、
  线程级 Case Correlation，并取消 SN-only Case 复用。
- AI-1 基础：版本化 AIProposal Schema、Evidence/Question/Profile/命令安全 Validator、
  Shadow 审计持久化、baseline 差异、Gateway 降级；Validator 强制 AI Hypothesis 为
  `OPEN`、`confirmable=false`、`L5` 且置信度不高于 0.75。
- Shadow 开启时正式 Reasoner 强制保持 Deterministic，Proposal 不进入正式 Hypothesis、
  Plan、状态机或设备 Action。
- FEISHU-AI-001/002/005 基础：飞书消息按 `message_id` 优先幂等；来源 tenant、会话、
  message/root/parent、sender、timestamp、normalized text 和 attachment refs 持久化；确定性
  Intent Router 覆盖八类合同意图并对低信息输入追问；文件/图片/音频/视频/富文本附件先
  下载登记 Evidence，附件路径不启动 Reproduction；“现象+设备”先进入确定性诊断和最小
  只读采集。
- 群聊和私聊为并列入口：`group` 与 `p2p` 均支持文本 Intake、附件 Evidence First、
  原线程 Case Correlation、状态查询、安全停止和原会话回复；私聊无需 @ 机器人。
- FEISHU-AI-004/006 基础：已接入 `ACCEPTED/CASE_CREATED/ATTACHMENT_READY/WAITING_USER/
  COMPLETED/FAILED` 六类标准反馈；Case 里程碑按类型和 token 幂等；状态查询
  转换为用户可理解的中文阶段；`WAITING_USER` 每轮只输出一个现场问题，
  不对外暴露 PCM/SLIC/aimd/SIP/RTP 内部问题；原线程回复幂等登记为
  `USER_RESPONSE` Evidence 并恢复对应等待中诊断。
- FEISHU-AI-002/003 增强：文本“现场操作已完成”可推进唯一等待中的实验 Run；
  文本“已修复”只在已确认根因且有根因引用时登记 FixAction，有可比基线
  Call 时同时创建 FixVerification；跨线程只在同会话、24 小时内“精确设备身份+
  具体症状”同时命中时关联，同分多候选必须要求用户给出 Case 编号。
- 附件处理已支持部分成功：成功文件继续登记 Evidence 和诊断，失败文件按文件名/
  阶段返回并引导用户在原线程重发；同消息同 `file_key` 不重复生成 Evidence。
- 多模态附件基础 Analyzer 已接入确定性诊断调度：WAV/OGG/Opus 等常见录音经 FFmpeg
  受限解码后可提取时长、采样率、
  声道、RMS/Peak、削波、原始静音段、Click/Pop 与窄带音调候选；所有现场录音异常均为
  `OPEN/L3` 候选，未与 SIP/RTP/终端 PCM 时间轴对齐前不得确认系统根因。PNG/JPEG/GIF/
  WebP 可校验文件头与基础元数据（不代表完整像素可解码）；Tesseract OCR 可提取脱敏
  中英文文字及注册/Codec/版本/告警候选，但固定为 L4，禁止将 OCR 候选直接当作设备事实。
  几何边缘、可能连接布局与颜色桶只生成 L4 视觉候选，禁止自动确认拓扑或将颜色解释为告警。
  Raw PCM 只在显式提供采样率、位宽、声道、符号和字节序后解析，参数不全时不做猜测。
- 同一 Case 的现场录音和 PCAP Media Artifact 可通过确定性信号相关输出时间偏移、绝对抓包
  时间和 Call-ID 映射；只有 MEDIUM/HIGH 相关才展示，且不得凭相关性单独确认故障因果。
- `GENERAL_QUESTION` 已接入确定性知识问答：只允许使用 `ACTIVE+verified`
  KnowledgeItem，答案必须返回知识条目标题和 `source_ref`；无足够匹配时必须拒绝
  猜测并引导用户进入故障诊断。当前路径不依赖未配置的模型。
- FixVerification 创建后已通过幂等 Worker 创建专用 ReproductionSession，继承基线
  设备和 Profile，并在 Cleanup Verified 后关联最新 `ANALYZED` Call 执行确定性修复评估。
  评估证据不完整时保持延后，不会把未验证的修复标记为成功。

本修订在开发环境已进一步落地：

- AI-F01/F02/F03/F05 只读工作台：每个诊断 Snapshot 生成 Evidence Quality、
  Contradiction Critic、注册 Question/Profile 建议和现场/技服/研发/客户/教学五种解释；
  结果持久化审计，`formal_result_changed=false`，不自动创建 ReproductionSession。
- Shadow Web 对比与 Eval API：展示 Proposal、Validator、baseline diff、延迟和降级；
  Question/Profile/解释接受或拒绝可追加记录，Eval 报告包含接受率和五个硬零指标。
- 多模型 Gateway 路由可按注册模型顺序故障切换，每次选择及 failover 随结果审计；
  外发摘要会脱敏 Secret/IP/MAC/号码和 Prompt Injection 文本。
- AI-F08/F09/F10/F11～F14 开发基础：版本回归/问题组/知识冲突只生成 L4 候选；
  截图除 OCR 外可生成几何、连线密度和颜色桶 L4 候选，不自动赋予告警含义；
  Rule/Profile/Knowledge/Code/Regression 输出强制持久化为不可执行、不可发布的 DRAFT。
- Raw PCM 可通过显式 sample rate/width/channels/signed/endian 参数导入；缺任一参数时
  fail closed，系统不猜格式。
- 当前有 8 个多模态开发 Golden 和 19 类 AI Shadow/安全/降级 Eval 场景。

尚未能由代码自主完成的是外部验收：真实飞书租户消息、真实设备/现场样本、
真实历史 Case 数据集和真实 Reasoning Gateway 效果、延迟与成本数据。它们属于
现场/Eval Gate 输入，不再是未实现的代码路径。

当前开发环境 `AI_SHADOW_ENABLED=false`，且未配置真实 Reasoning Gateway，因此只读工作台、
合成 Eval、路由和降级均可自测，但仍不得开启 AI 自动动作。

在上述能力逐项通过 Gate 前，必须保持当前确定性链路为正式结果来源。

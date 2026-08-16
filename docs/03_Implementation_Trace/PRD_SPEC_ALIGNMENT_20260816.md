# PRD / SPEC 开发环境对齐结果

日期：2026-08-16  
范围：现有开发环境，不包含生产部署、生产密钥和对真实现场效果的虚假声明。

## 结论

PRD V1.0/V1.1、SPEC V1.1/M6.2 与 SPEC V1.2 中可在当前开发环境实现的代码路径
已补齐。正式根因、因果确认、Fix Verification、设备动作和 Cleanup 仍由确定性
Reasoner/Rule/Gate/Profile/Orchestrator 负责；AI 输出不能直接修改状态机或执行命令。

| 合同域 | 开发环境结果 | 主要验证 |
|---|---|---|
| 飞书群聊 @ / 机器人私聊 | 已对齐 | 同一 Intake、Case Correlation、附件、停止、状态和回复合同 |
| Evidence First / 最小用户追问 | 已对齐 | 附件先分析；每轮一个用户问题；不询问 PCM/SLIC/SIP/RTP 内部字段 |
| 确定性 SIP/RTP/PCM/Audio/FXS | 已对齐 | Rule、Golden、Synthetic E2E、Reproduction/Cleanup/Fix Gate |
| M6.2 WATCH / Capture / Call Binding | 已对齐 | FXS_MONITOR_READY fail-closed、Ring Segment、Anchor、Cleanup |
| AI Proposal Shadow | 已对齐 | Schema、跨 Case Evidence、未注册项、命令、降级和不合并正式结论 |
| AI-F01 Quality / AI-F02 Critic | 已对齐 | 只读侧车持久化，Hard/Soft/Unsupported/Missing 结构化输出 |
| AI-F03 Question / Profile 规划 | 已对齐 | 只选注册项，后端重算，说明区分目标、证据、耗时、风险，不自动复现 |
| AI-F05 角色化解释 | 已对齐 | 现场/技服/研发/客户/教学五版保持同一事实、等级和 Evidence ID |
| AI-F08/F09 版本与问题组 | 已实现候选路径 | 仅 L4/CANDIDATE，不单独确认因果 |
| AI-F10 多模态 | 已对齐开发基础 | Opus/OGG、Raw PCM 参数化、OCR 脱敏、视觉 L4 候选、录音与 RTP/PCM 对齐 |
| AI-F11～F14 工程 Copilot | 已对齐安全边界 | Rule/Profile/Knowledge/Code/Regression 只能持久化为不可执行 DRAFT |
| Gateway 隐私/多模型/降级 | 已对齐 | 结构化摘要、Secret/IP/MAC/号码/Injection 脱敏、模型顺序 failover |
| Shadow Web / Eval Gate | 已对齐代码路径 | Proposal/Diff/Validator/延迟、接受率反馈、五个硬零指标、19 类 Eval Golden |

## 仍需外部输入的验收

以下项不能通过生成数据或 Mock 宣称已完成，但对应接入、降级、审计和 Gate 已实现：

- 真实飞书租户的群聊/私聊消息与真实原生语音。
- 真实设备截图、成对现场录音/PCAP 和复杂失真样本。
- 真实历史 Case Eval 数据集。
- 真实 Reasoning Gateway 凭证、模型输出、延迟和成本。
- 生产 Secrets/Auth/CORS/MinIO/发布验收；当前用户已明确不在本阶段部署生产。

## 自测口径

- 后端全量 `pytest` 。
- Frontend `npm ci && npm run build`。
- Migration/OpenAPI/Profile/Question/Experiment/Rule/Workbench/AI Eval/Security 门禁。
- Synthetic Golden、Synthetic E2E 与 baseline diff。
- Docker 重建后的 health、Alembic head、Celery 注册和无活动任务检查。

## 最终自测结果

- Backend：361 passed。
- Frontend：`npm ci && npm run build` PASS。
- Phase F3 严格静态门禁：22/22 PASS，并已修复原脚本吞掉管道失败的问题。
- Migration：17 migrations，single head `0017_ai_recommendation_feedback`。
- OpenAPI：83 paths / 90 operations，冻结合同 PASS。
- Reproduction：Mock 3/3、Evidence 5/5、Experiment/Causal 4/4 PASS。
- Synthetic Golden：21/21 PASS。Synthetic E2E：53/53 PASS，0 regression。
- AI Eval Golden：19/19 类别覆盖 PASS。多模态 Golden：8 个开发基线。
- 开发容器：Backend/Frontend/Worker/PostgreSQL/Redis/MinIO 运行正常；
  Celery active/reserved/scheduled 均为空，`WATCHING=0`，活动设备锁为 0。
- AI 运行态 Smoke：只读工作台成功返回 Quality/Critic/Question/Profile/五角色解释，
  `formal_result_changed=false`；推荐反馈成功进入 Eval 接受率。

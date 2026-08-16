# VOIP AI 故障助手 SPEC V1.1：复现采集架构修订

状态：APPROVED CHANGE / 2026-08-15  
基线：`VOIP_AI_故障助手_SPEC_V1.0_终稿.docx`

后续修订：飞书群聊入口、AI Intake、AI Proposal、用户问答与模型治理见
`VOIP_AI_故障助手_SPEC_V1.2_AI诊断与飞书入口修订.md`。本文件继续作为复现采集、
Readiness、Signal Observer、Cleanup 和 Evidence Locator 的权威规格。

## 1. 单一持续采集流

真实平台在进入 WATCHING 前启动指定 Voice 接口的连续、分段、全 UDP Ring。
同一 packet stream 同时服务于 Hot Ring、Segment Writer、Capture Health、PCM
数据面验证和 SIP/RTP Signal Observer。禁止使用重复短窗口 tcpdump 作为唯一
Call/Media 判定依据。

平台不支持 SSH 流式读取时，允许使用单实例 DUT rotating PCAP 后下载；任何时刻
不得为 40000/50000 分别启动竞争性的点采样进程。

Rotating PCAP 的 packet producer 必须独立于文件下载：文件封口后立即开始下一段，
Base64/SFTP 传输只能消费 ready 文件，不得位于两段 tcpdump 之间。出现消费积压时，
downloader 必须用有界压缩批次持续消费，单批不得超过命令超时/内存上限；收到
End Anchor 后先 seal producer，再循环 drain 全部 tail 文件，禁止在 tail drain 未完成时
Finalize/Cleanup 或把 Capture 标记为 COMPLETE。若相邻 Segment
存在超过容差的采集空洞，跨空洞的 RTP sequence jump 必须标记为 capture gap，不能
提升为网络丢包 Finding。

## 2. 两阶段 Readiness

除数据面两阶段 Readiness 外，控制面必须设置 WATCH runtime barrier：AIM/FXS debug
命令逐条获得 prompt acknowledgement、reader future 存活后发布 `FXS_MONITOR_READY`。
reader 退出必须发布 `FXS_MONITOR_FAILED`、将 Debug Health 置 FAILED 并 fail closed。

`ArmBarrierConfig.readiness_mode`：

- `PREWATCH_DATA_PLANE`：严格模式，WATCHING 前必须存在配置包数的数据流。
- `ACTIVITY_GATED`：空闲静默平台模式。ARM 验证 Capture Path；首次 Attempt 后
  在 `first_activity_validation_seconds` 内验证真实 PCM 数据面。

状态通过 ArmValidation Evidence 的 `readiness_phase` 表达：

- `CAPTURE_PATH_READY`
- `DATA_PLANE_VERIFIED`
- `CAPTURE_PATH_DEGRADED`
- `NOT_READY`

命令返回成功不能标记 `DATA_PLANE_VERIFIED`。

## 3. Signal Observer 与绑定等级

Signal Observer 为确定性组件，不依赖 LLM：

| 信号 | 语义 |
|---|---|
| FXS OFFHOOK | Attempt Start |
| SIP INVITE | 首选 Call Binding |
| 平台 Call Connected | 平台确定性 Call Binding |
| progressing RTP v2 flow | `RTP_STREAM_START_FALLBACK` |
| PCM 40000/50000 | Capture Health，不绑定 Call |
| ONHOOK/BYE/RTP Idle | End Anchor |

每个绑定必须保存 source、event type、external reference、Evidence/Segment 和时间来源。
绑定时间必须取触发绑定的首个 SIP/RTP packet timestamp，并映射到 Segment 的 Session
相对时间区间；禁止使用 Segment 完成时间代替绑定时间。存在待处理 End Anchor 时，
`Call Binding <= End Anchor` 是创建 Call 的硬约束。

`ECHO_PATH`、`DTMF_PATH` 表示路径可观测，不天然表示故障。通用 Profile 不得仅凭
路径存在输出 MATCH 或故障假设；只有对应症状 Profile，或号码不一致、质量门限越界等
独立异常证据，才允许提升为故障结论。

同理，单个 PCM Session 的 hum/silence/click 候选在无对应用户症状、无活跃媒体窗口
对齐或无跨层传播证据时，只能作为上下文，不能成为 `SUPPORTED` 故障或报告 headline。

## 4. 执行面隔离

- `reproduction-control-high`：Cancel、Cleanup、Recovery、Reconcile。
- `reproduction-control`：Start、Finalize、Retry、Enhancement。
- `reproduction-watch`：长时 FXS/Packet Ring watcher。

Cleanup/Recovery 不得排在长时 watcher 后面。同一 DUT 仍由 DeviceDiagnosticLock
保证单 Active Session，不同 DUT 可并行。三个队列分别由独立 Worker 消费；尤其
`reproduction-control-high` 不得与 Start 或 watcher 共用并发槽。

## 5. Cleanup Safety Interlock

Cleanup 成功后释放锁。Cleanup 失败时锁转换为 `QUARANTINED`：

- 禁止任何新 Session ARM。
- Cleanup/Recovery 可继续访问该 DUT。
- 反向验证成功后解除隔离并释放锁。

设备命令无需假定天然幂等；系统通过动作账本、执行前探测和执行后数据面验证
提供语义幂等。

## 6. Evidence Object Locator

Evidence 读取支持 Hot/Staging/Permanent：

- Live Analyzer 读取当前 Segment。
- Full Quick Analyzer 读取冻结 Staging。
- Finalize 完成 checksum/manifest/integrity 后上传 Permanent MinIO。
- Classic Analyzer 通过统一 Locator 解析后端，禁止固定假设对象已在 MinIO。

## 7. 外部状态兼容

现有 Frozen API 状态机继续保留；内部 Readiness、Capture Health、Cleanup Safety
通过正交字段和 Evidence 表达。任何退出路径仍必须经过 CLEANUP/Recovery，不能从
CAPTURING 直接进入终态。

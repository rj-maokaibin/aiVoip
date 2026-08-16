# M6.2 Reproduction Intelligence SPEC V1.1 修订

状态：APPROVED CHANGE / 2026-08-15  
替代范围：V1.0 SPEC-03、05、08、15、16、30、31、33 的下列冲突条款；未列出的
V1.0 条款继续有效。

## M62-CR-001：Activity-gated Data-plane Readiness

对于经 EC-02/PlatformProfile 验证为空闲静默的 PCM 能力，允许在
`CAPTURE_PATH_READY` 后进入 WATCHING。首次业务活动后必须在 Profile 配置窗口内
完成 40000/50000 验证；失败产生 `PCM_*_NOT_VERIFIED` Evidence，并驱动
CAPTURE_RECOVERY/PARTIAL。严禁将命令成功伪装为真实数据面健康。

## M62-CR-002：Continuous Ring + Signal Fusion

Ring 必须在 Anchor 前开启。Call Binder 使用 SIP INVITE、平台确定性 Call State、
RTP progression 的有序信号集合；PCM 不作为独立 Call Binding。ONHOOK 无 Call 时，
必须先分析覆盖 End Anchor 的在途/冻结 Segment，再结束 Attempt。

Call Binding Anchor 必须来自实际触发 packet，而非 Segment End；映射后的 Session
相对时间不得越出 Segment，且不得晚于已观测的 End Anchor。事件 source 必须反映
实际平台（REAL/MOCK），不得使用测试平台标记污染真实证据。

路径存在与故障成立必须分离：DTMF 解码成功、RX/TX 延迟相关只构成上下文 Finding。
通用采集不得据此判定 TARGET；DTMF mismatch、对应症状上下文或其他独立异常证据
才可形成故障假设。

通用采集中的 PCM hum/silence/click 候选同样不得脱离症状与通话窗口直接形成
`SUPPORTED` 故障；无症状候选必须保留为 context/known fact。

## M62-CR-003：Cleanup Priority and Quarantine

Cleanup/Recovery 使用独立高优先级执行面。Cleanup Failed 时 DUT 进入
`DIAGNOSTIC_QUARANTINED`，禁止新 ARM，但不阻止 Evidence 分析和 Cleanup Recovery。
Recovery 反向验证成功后才能解除隔离。

## M62-CR-004：Tier-aware Evidence Resolution

Hot Ring 可覆盖；Freeze 后 Staging 不可覆盖；Permanent 由 Finalize manifest
确认。Quick Analyzer 可读取 Staging，Diagnosis 优先读取 Finalized Bundle。所有
Analyzer 必须通过统一 Evidence Locator，不得固定绑定 MinIO 或本地路径。

## M62-CR-005：分层 Release Gate

M6.2-A Reliable Reproduction Core：Ring、Attempt/Call/Media、Cancel/Timeout、Cleanup、
Crash Recovery、Immutable Evidence。  
M6.2-B Diagnostic Intelligence：CONTROL/TARGET、Live/Full、Sufficiency、Enhancement。  
M6.2-C Causal Verification：A/B、A-B-A、Environment Gate、Fix Verification。

后续层不得掩盖 M6.2-A 的稳定性失败。完整 V1.0 验收仍要求 A/B/Fix Verification，
但生产启用必须逐层通过 Gate。

对于 SSH 文件式采集，M6.2-A 的 Ring 要求 DUT 侧 producer 连续运行，文件下载不得
暂停 producer；downloader 必须使用不超过单命令超时/内存上限的有界批次消费 ready
文件。End Anchor 到达时必须先 seal producer 并循环排空包含原 open file 在内的
tail backlog，排空完成前不得声明
Capture COMPLETE 或进入 Cleanup。已知 capture gap 上的 RTP sequence jump 不构成网络丢包证据。

## 更新后的核心验收

1. Ring 在 OFFHOOK 前已有有效 Segment。
2. PCM burst 只出现在首个活动窗口时仍能验证数据面。
3. PCM-only Attempt 不得创建 Call。
4. SIP INVITE 或 progressing RTP 可确定性创建/重建 Call。
5. ONHOOK 与 Segment 完成竞态不得丢失 Call Evidence。
6. WATCH runtime 必须在 AIM debug 命令逐条 prompt acknowledgement 且 reader 存活后
   发布 `FXS_MONITOR_READY`；仅有 Session `WATCHING` 不构成可操作条件。
6. Cancel/Watch Timeout 不受 watcher 队列阻塞。
7. Cleanup BrokenPipe 可重建 AIM PTY并重试；不重复已确认执行的非幂等 OFF。
8. Cleanup Failed 自动隔离 DUT。
9. Staging Evidence 可被 Quick/Classic Analyzer 读取。
10. Call Binding 时间不晚于 ONHOOK，真实 FXS Anchor source 为 REAL_PLATFORM。
11. 正常通话即使存在 DTMF/echo path observation，也不得被通用 Profile 判为故障。
12. 无噪声症状时，单点 hum candidate 不得成为 Diagnosis headline。

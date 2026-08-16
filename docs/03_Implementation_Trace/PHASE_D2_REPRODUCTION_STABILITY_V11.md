# Phase D2 — Reproduction Stability V1.1

日期：2026-08-15

## 现场触发原因

真实 Session `0010149f-06d0-402f-b150-1028e0403b95` 的前两个 Attempt 均捕获到
UDP/40000 与 UDP/50000 各 407 帧，但短窗口 `pcm_media_active()` 在同步
post-anchor capture 结束后返回假阴性，Call 未绑定。长时 watcher 同时独占单并发
reproduction queue，Cancel/Reconcile 无法执行。

## 实施内容

- Profile 增加 `ACTIVITY_GATED` 两阶段 Readiness。
- Real Platform ARM 使用动态 RuntimeContext，PCM 空闲期标记 STARTING/PENDING。
- WATCHING 前启动异步全 UDP Segmented Ring。
- Ring 采用 DUT 侧单一持续 producer：producer 连续封口 PCAP 文件，SSH downloader
  以“单文件 gzip + Base64”的有界批次消费 ready 文件，传输不阻塞下一段 tcpdump。
  若隧道吞吐低于生产速率，允许 DUT 侧形成 backlog；ONHOOK 先 seal producer，再循环
  tail drain 到 remaining=0。覆盖 End Anchor 的全部文件持久化后才允许 Finalize/Cleanup。
- APF3260-M 使用 tcpdump 4.99.4 原生 `-G` 单进程轮转；BusyBox
  `start-stop-daemon` 管理 producer PID。空闲期无 sealed file 的轮询结果不进入
  Evidence/merge；已封口但仅含 24-byte global header、0 个 UDP 包的空 PCAP 同样
  标记为 EVICTED，避免空段进入完整通话合并。
- 新增确定性 PCAP Signal Observer；PCM 只更新 Capture Health。
- ONHOOK 延迟到在途 Segment 完成后再判定 no-call。
- 拆分 control-high/control/watch 队列。
- watcher 使用 Profile timeout 和持久化 Heartbeat。
- watcher 每轮强制刷新跨 Worker 的 Session 状态；高优先级 Cancel 提交后立即停止
  在途 Segment/Heartbeat，Cancel 与 Heartbeat 的极窄竞态按正常外部终止处理，不再
  误报 `REPRODUCTION_LEASE_EXPIRED` 或触发任务重试。
- Cleanup 失败将 Device Lock 转换为 QUARANTINED。
- AIM BrokenPipe 纳入 PTY 重建重试。
- AIM 在输出 Prompt 后、首条 Debug 命令前关闭时，`write_aim`/stream reader 会丢弃
  stale PTY 并有界重建；该启动路径统一转换为可重试的设备命令错误。
- Analyzer 通过 tier-aware Evidence materializer 读取 Staging/MinIO。
- Binder 保存首个 SIP/RTP packet timestamp，并将其映射为 Session Anchor；拒绝
  End Anchor 之后的尾包绑定。
- 真实 OFFHOOK/ONHOOK 与 Cleanup metadata 使用真实 Platform identity。
- CALL_QUICK 将 DTMF/echo path presence 与 fault anomaly 分离；Generic Profile 不再
  因路径存在产生 MATCH，Diagnosis 仅在对应症状上下文中提升路径假设。

## 首轮真机验证与闭环

Session `47d4db8f-b993-4106-a25b-a022e9fa3b1e` 已完成 OFFHOOK → 拨号 301 →
通话 → ONHOOK → Finalize → Diagnosis：双向 PCM 各 815 包、progressing RTP 610 包，
Call、Cleanup、Manifest 与 Diagnosis 均成功。现场同时发现并修复三项语义缺陷：

1. 真实 FXS Event 被错误标记为 `MOCK_PLATFORM`；
2. Call Binding 使用 Segment End，可能晚于 ONHOOK；
3. Generic Profile 将路径可观测误判为回声/DTMF故障。

上述三项均加入回归测试与本修订验收条款。

第三轮验证进一步发现旧实现按“抓包→Base64 下载→下一段”串行执行，相邻 PCAP
实际存在约 3.64s/10.31s 空洞，并被 RTP sequence analyzer 误识别为 BURST_LOSS。
现已替换为上述持续 producer；该轮 `RTP_BURST_LOSS` 结论标记为采集伪影，不作为
真实网络故障证据。后续真机验收要求相邻非空 Segment 的 packet timestamp 连续，
间隙不得超过配置的 capture-gap tolerance。

最终原生轮转验证的两个有效通话段边界 packet gap 为约 `9.93ms`；CALL_QUICK 为
`INCONCLUSIVE`，不含 `RTP_BURST_LOSS`/`ECHO_PATH`。同时将无 `AUDIO_NOISE`
症状的 HIGH hum score 降为 context，防止正常通话报告产生错误故障 headline。

长通话 R02 首轮进一步发现 producer 连续但 downloader 每轮仅消费一个 ready 文件，
约 53 秒 Attempt 最终只持久化约 20 秒媒体，且会话被错误标记 COMPLETE。第二轮将
全部 backlog 合并为一个 Base64 响应，又超过 30 秒 SSH command timeout。现改为上述
有界压缩批次 + End Anchor 循环 tail drain；前两轮均不计通过。

R02 第三轮 12 个有效媒体段全部排空，最大 packet boundary gap `9.988ms`，无 SSH
timeout/retry，tail 覆盖到主 ONHOOK 前约 1.1 秒。该轮同时观测到主 ONHOOK 后约
0.5 秒的 hook bounce；End Anchor 改为 first-edge latch，后续重复 ONHOOK 不得覆盖。

H03 首轮两次真实摘机均未产生 FXS Event，而 PCAP/Heartbeat 仍健康，确认 AIM reader
可“进程存活但 debug enable 未可靠生效”。WATCH runtime readiness 改为逐条 AIM 命令
prompt acknowledgement 后才发布 `FXS_MONITOR_READY`；reader future 纳入持续 Health，
异常退出发布 `FXS_MONITOR_FAILED` 并 fail closed，不得继续伪装为可监听。

H03 prompt-verified 重测正确得到约 8.9 秒 OFFHOOK→ONHOOK、0 DTMF、0 Call，Attempt
按 no-call INVALID 返回 WATCHING。该轮又发现 End Anchor drain 后 ring 保持 sealed，
导致空 downloader 紧循环；现规定 no-call 返回 WATCHING 前删除 sealed ring 并启动新
idle producer，后续 Attempt 不得复用已封口目录。

## 保留边界

- 同 DUT 单 Active Session。
- Profile/Action 白名单与 Cleanup 对称性。
- 所有退出路径进入 Cleanup/Recovery。
- Raw Segment immutable，Finalize 生成 manifest。
